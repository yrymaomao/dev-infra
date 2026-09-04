"""Transactional batch queue, snapshots, and activity persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from agent_runtime.payloads.contracts import PayloadAuthorizationError, PayloadStore
from pydantic import JsonValue
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .activity import ActivityProjection
from .contracts import BatchCreateRequest
from .eta import EtaEstimate, EtaEstimator, EtaProfile
from .models import Batch, BatchActivity, BatchItem
from .result_contract import OutputContractError, public_result, validated_result
from .runtime_client import RuntimeRequestError, RuntimeStartResult

_PAYLOAD_PERMISSION = "supply-chain:bff-dispatch"
_ACTIVE_ITEM_STATUSES = ("DISPATCHING", "DISPATCHED", "RUNNING", "WAITING")
_TERMINAL_ITEM_STATUSES = ("SUCCEEDED", "BLOCKED", "FAILED", "CANCELLED")


@dataclass(frozen=True, slots=True)
class ClaimedItem:
    item_id: UUID
    batch_id: UUID
    tenant_id: str
    item_index: int
    attempt_count: int
    lease_owner: str
    snapshot_time: datetime
    payload_ref: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class BatchPayload:
    skus: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActiveExecution:
    tenant_id: str
    batch_id: UUID
    execution_id: UUID
    sequence: int


class BatchRepository:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        payload_store: PayloadStore,
        tenant_concurrency: int,
        global_concurrency: int,
        eta_profile: EtaProfile,
    ) -> None:
        self._factory = factory
        self._payload_store = payload_store
        self._tenant_concurrency = tenant_concurrency
        self._global_concurrency = global_concurrency
        self._eta_profile = eta_profile

    async def create_batch(
        self,
        *,
        tenant_id: str,
        request: BatchCreateRequest,
        eta: EtaEstimate,
        now: datetime,
    ) -> UUID:
        staged = await self._payload_store.put_exact_restricted(
            tenant_id=tenant_id,
            payload=cast(
                JsonValue,
                {"skus": list(request.skus)},
            ),
            required_permission=_PAYLOAD_PERMISSION,
        )
        if staged.payload_ref is None:
            raise RuntimeError("BFF batch payload must use external controlled storage")
        batch_id = uuid4()
        try:
            async with self._factory() as session, session.begin():
                session.add(
                    Batch(
                        id=batch_id,
                        tenant_id=tenant_id,
                        client_request_id=request.client_request_id,
                        status="ACCEPTED",
                        sku_count=len(request.skus),
                        completed_count=0,
                        failed_count=0,
                        marketplace=request.marketplace,
                        fulfillment_mode=request.fulfillment_mode,
                        snapshot_time=now,
                        eta_profile_version=eta.profile_version,
                        eta_low_seconds=eta.low_seconds,
                        eta_high_seconds=eta.high_seconds,
                        payload_ref=staged.payload_ref,
                        payload_hash=staged.payload_hash,
                    )
                )
                session.add_all(
                    [
                        BatchItem(
                            tenant_id=tenant_id,
                            batch_id=batch_id,
                            item_index=index,
                            sku_hash=hashlib.sha256(sku.encode("utf-8")).hexdigest(),
                            status="PENDING",
                            attempt_count=0,
                            next_attempt_at=now,
                        )
                        for index, sku in enumerate(request.skus)
                    ]
                )
                session.add(
                    BatchActivity(
                        tenant_id=tenant_id,
                        batch_id=batch_id,
                        event_key="batch:accepted",
                        event_type="batch.accepted",
                        phase="validation",
                        state="accepted",
                        progress_current=0,
                        progress_total=len(request.skus),
                        safe_message=f"Batch accepted with {len(request.skus)} SKU(s).",
                        payload={"schema_version": "business-agent.activity-event.v1"},
                        critical=True,
                    )
                )
        except IntegrityError:
            async with self._factory() as session:
                existing = await session.scalar(
                    select(Batch.id).where(
                        Batch.tenant_id == tenant_id,
                        Batch.client_request_id == request.client_request_id,
                    )
                )
            if existing is None:
                raise
            return existing
        await self._payload_store.ensure_committed(
            tenant_id=tenant_id,
            payload_ref=staged.payload_ref,
            payload_hash=staged.payload_hash,
            size_bytes=staged.size_bytes,
            content_type=staged.content_type,
            classification=staged.classification,
            required_permission=staged.required_permission,
        )
        return batch_id

    async def claim_item(self, *, worker_id: str, now: datetime) -> ClaimedItem | None:
        async with self._factory() as session, session.begin():
            # Serialize the short scheduling decision so every claimant observes
            # committed global and tenant active counts before taking a row lease.
            await session.execute(text("SELECT pg_advisory_xact_lock(78974213601234016)"))
            await session.execute(
                update(BatchItem)
                .where(
                    BatchItem.status == "DISPATCHING",
                    BatchItem.lease_expires_at <= func.current_timestamp(),
                )
                .values(status="PENDING", lease_owner=None, lease_expires_at=None)
            )
            global_active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BatchItem)
                    .where(BatchItem.status.in_(_ACTIVE_ITEM_STATUSES))
                )
                or 0
            )
            if global_active >= self._global_concurrency:
                return None
            candidates = (
                await session.scalars(
                    select(BatchItem)
                    .join(
                        Batch,
                        (Batch.tenant_id == BatchItem.tenant_id) & (Batch.id == BatchItem.batch_id),
                    )
                    .where(
                        BatchItem.status == "PENDING",
                        BatchItem.next_attempt_at <= now,
                        or_(
                            BatchItem.lease_expires_at.is_(None),
                            BatchItem.lease_expires_at <= func.current_timestamp(),
                        ),
                        Batch.cancel_requested.is_(False),
                    )
                    .order_by(Batch.created_at, BatchItem.item_index)
                    .limit(32)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for item in candidates:
                tenant_active = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(BatchItem)
                        .where(
                            BatchItem.tenant_id == item.tenant_id,
                            BatchItem.status.in_(_ACTIVE_ITEM_STATUSES),
                        )
                    )
                    or 0
                )
                if tenant_active >= self._tenant_concurrency:
                    continue
                batch = await session.scalar(
                    select(Batch).where(
                        Batch.tenant_id == item.tenant_id,
                        Batch.id == item.batch_id,
                    )
                )
                if batch is None:
                    continue
                item.status = "DISPATCHING"
                item.attempt_count += 1
                item.lease_owner = worker_id
                item.lease_expires_at = now + timedelta(seconds=30)
                if item.started_at is None:
                    item.started_at = now
                batch.status = "RUNNING"
                return ClaimedItem(
                    item_id=item.id,
                    batch_id=item.batch_id,
                    tenant_id=item.tenant_id,
                    item_index=item.item_index,
                    attempt_count=item.attempt_count,
                    lease_owner=worker_id,
                    snapshot_time=batch.snapshot_time,
                    payload_ref=batch.payload_ref,
                    payload_hash=batch.payload_hash,
                )
        return None

    async def load_payload(self, claim: ClaimedItem) -> BatchPayload:
        try:
            data = await self._payload_store.get_authorized(
                tenant_id=claim.tenant_id,
                payload_ref=claim.payload_ref,
                permission_scope=_PAYLOAD_PERMISSION,
            )
        except PayloadAuthorizationError:
            staged = await self._payload_store.inspect_for_finalization(
                tenant_id=claim.tenant_id,
                payload_ref=claim.payload_ref,
                permission_scope=_PAYLOAD_PERMISSION,
            )
            if staged.payload_hash != claim.payload_hash:
                raise ValueError("batch payload identity mismatch")
            await self._payload_store.ensure_committed(
                tenant_id=claim.tenant_id,
                payload_ref=claim.payload_ref,
                payload_hash=staged.payload_hash,
                size_bytes=staged.size_bytes,
                content_type=staged.content_type,
                classification=staged.classification,
                required_permission=staged.required_permission,
            )
            data = await self._payload_store.get_authorized(
                tenant_id=claim.tenant_id,
                payload_ref=claim.payload_ref,
                permission_scope=_PAYLOAD_PERMISSION,
            )
        if hashlib.sha256(data).hexdigest() != claim.payload_hash:
            raise ValueError("batch payload hash mismatch")
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("skus"), list)
            or not all(isinstance(sku, str) for sku in value["skus"])
            or claim.item_index >= len(value["skus"])
        ):
            raise ValueError("batch payload contract is invalid")
        return BatchPayload(skus=tuple(value["skus"]))

    async def record_start(self, claim: ClaimedItem, result: RuntimeStartResult) -> None:
        async with self._factory() as session, session.begin():
            item = await self._owned_item(session, claim)
            if item is None:
                return
            item.runtime_execution_id = UUID(result.execution_id)
            item.runtime_session_id = UUID(result.session_id) if result.session_id else None
            item.status = "DISPATCHED"
            item.lease_owner = None
            item.lease_expires_at = None
            batch = await self._batch(session, claim.tenant_id, claim.batch_id)
            batch.runtime_mode = result.mode
            await self._insert_activity(
                session,
                tenant_id=claim.tenant_id,
                batch_id=claim.batch_id,
                item_id=claim.item_id,
                execution_id=item.runtime_execution_id,
                event_key=f"item:{claim.item_id}:dispatched",
                event_type="item.dispatched",
                phase="dispatch",
                state="running",
                safe_message="SKU analysis dispatched.",
                critical=True,
            )

    async def record_runtime_event(
        self,
        *,
        tenant_id: str,
        batch_id: UUID,
        projection: ActivityProjection,
    ) -> None:
        async with self._factory() as session, session.begin():
            item = await session.scalar(
                select(BatchItem).where(
                    BatchItem.tenant_id == tenant_id,
                    BatchItem.batch_id == batch_id,
                    BatchItem.runtime_execution_id == UUID(projection.execution_id),
                )
            )
            if item is None or projection.execution_sequence <= item.runtime_sequence:
                return
            item.runtime_sequence = projection.execution_sequence
            previous_status = item.status
            if projection.state == "waiting":
                item.status = "WAITING"
            elif projection.state in {"running", "progress"}:
                item.status = "RUNNING"
            await self._insert_activity(
                session,
                tenant_id=tenant_id,
                batch_id=batch_id,
                item_id=item.id,
                execution_id=item.runtime_execution_id,
                event_key=projection.event_key,
                event_type=projection.event_type,
                phase=projection.phase,
                state=projection.state,
                safe_message=projection.safe_message,
                tool_name=projection.tool_name,
                duration_ms=projection.duration_ms,
                payload=projection.payload,
                execution_sequence=projection.execution_sequence,
                critical=projection.critical,
            )
            if item.status != previous_status:
                await self._refresh_batch(session, tenant_id=tenant_id, batch_id=batch_id)

    async def record_snapshot(
        self,
        *,
        tenant_id: str,
        execution_id: UUID,
        snapshot: dict[str, Any],
    ) -> None:
        status = str(snapshot.get("status", ""))
        if status not in _TERMINAL_ITEM_STATUSES:
            async with self._factory() as session, session.begin():
                item = await session.scalar(
                    select(BatchItem).where(
                        BatchItem.tenant_id == tenant_id,
                        BatchItem.runtime_execution_id == execution_id,
                    )
                )
                if item is not None and status in {
                    "RUNNING",
                    "WAITING_EXTERNAL",
                    "WAITING_APPROVAL",
                }:
                    item.status = "WAITING" if status.startswith("WAITING") else "RUNNING"
                    await self._refresh_batch(
                        session,
                        tenant_id=tenant_id,
                        batch_id=item.batch_id,
                    )
            return
        contract_error: OutputContractError | None = None
        if status == "SUCCEEDED":
            try:
                result = validated_result(snapshot)
                if result.get("status") == "BLOCKED":
                    status = "BLOCKED"
            except OutputContractError as caught:
                contract_error = caught
                status = "FAILED"
        staged = await self._payload_store.put_exact_restricted(
            tenant_id=tenant_id,
            payload=cast(JsonValue, snapshot),
            required_permission=_PAYLOAD_PERMISSION,
        )
        if staged.payload_ref is None:
            raise RuntimeError("terminal snapshot must use controlled storage")
        await self._payload_store.ensure_committed(
            tenant_id=tenant_id,
            payload_ref=staged.payload_ref,
            payload_hash=staged.payload_hash,
            size_bytes=staged.size_bytes,
            content_type=staged.content_type,
            classification=staged.classification,
            required_permission=staged.required_permission,
        )
        async with self._factory() as session, session.begin():
            item = await session.scalar(
                select(BatchItem)
                .where(
                    BatchItem.tenant_id == tenant_id,
                    BatchItem.runtime_execution_id == execution_id,
                )
                .with_for_update()
            )
            if item is None or item.status in _TERMINAL_ITEM_STATUSES:
                return
            item.status = status
            item.result_ref = staged.payload_ref
            item.result_hash = staged.payload_hash
            item.completed_at = datetime.now(UTC)
            snapshot_error = snapshot.get("error")
            if contract_error is not None:
                item.error_phase = "output_validation"
                item.error_category = "contract"
                item.error_retryable = False
                item.safe_message = "The analysis result failed Output Schema validation."
                item.request_id = str(snapshot.get("trace_id") or "")[:256]
            elif isinstance(snapshot_error, dict):
                item.error_phase = str(snapshot_error.get("phase", "invocation"))[:32]
                item.error_category = str(snapshot_error.get("category", "permanent"))[:32]
                item.error_retryable = bool(snapshot_error.get("retryable", False))
                item.safe_message = str(snapshot_error.get("safe_message", "Analysis failed"))[
                    :1024
                ]
                item.request_id = str(
                    snapshot_error.get("request_id") or snapshot_error.get("trace_id") or ""
                )[:256]
            await self._insert_activity(
                session,
                tenant_id=tenant_id,
                batch_id=item.batch_id,
                item_id=item.id,
                execution_id=execution_id,
                event_key=f"item:{item.id}:terminal",
                event_type="item.terminal",
                phase="terminal",
                state=status.casefold(),
                safe_message=(item.safe_message or "SKU analysis completed."),
                critical=True,
            )
            await self._refresh_batch(session, tenant_id=tenant_id, batch_id=item.batch_id)

    async def record_dispatch_error(
        self, claim: ClaimedItem, error: RuntimeRequestError | Exception
    ) -> None:
        retryable = isinstance(error, RuntimeRequestError) and error.retryable
        async with self._factory() as session, session.begin():
            item = await self._owned_item(session, claim)
            if item is None:
                return
            if retryable and claim.attempt_count < 8:
                item.status = "PENDING"
                item.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=min(60, 2 ** max(claim.attempt_count - 1, 0))
                )
                state = "retrying"
                message = "Runtime is temporarily unavailable; retry scheduled."
            else:
                item.status = "FAILED"
                item.completed_at = datetime.now(UTC)
                state = "failed"
                message = (
                    error.safe_message
                    if isinstance(error, RuntimeRequestError)
                    else "Supply chain analysis could not be dispatched."
                )
            item.lease_owner = None
            item.lease_expires_at = None
            await self._insert_activity(
                session,
                tenant_id=claim.tenant_id,
                batch_id=claim.batch_id,
                item_id=claim.item_id,
                execution_id=None,
                event_key=f"item:{claim.item_id}:dispatch:{claim.attempt_count}",
                event_type="item.dispatch",
                phase="dispatch",
                state=state,
                safe_message=message,
                critical=True,
            )
            if item.status == "FAILED":
                await self._refresh_batch(
                    session, tenant_id=claim.tenant_id, batch_id=claim.batch_id
                )

    async def get_batch(self, *, tenant_id: str, batch_id: UUID) -> dict[str, Any] | None:
        async with self._factory() as session:
            batch = await session.scalar(
                select(Batch).where(Batch.tenant_id == tenant_id, Batch.id == batch_id)
            )
            if batch is None:
                return None
            items = (
                await session.scalars(
                    select(BatchItem)
                    .where(BatchItem.tenant_id == tenant_id, BatchItem.batch_id == batch_id)
                    .order_by(BatchItem.item_index)
                )
            ).all()
            projected_skus: dict[int, str] = {}
            try:
                raw_batch = await self._payload_store.get_authorized(
                    tenant_id=tenant_id,
                    payload_ref=batch.payload_ref,
                    permission_scope=_PAYLOAD_PERMISSION,
                )
                if hashlib.sha256(raw_batch).hexdigest() == batch.payload_hash:
                    batch_payload = json.loads(raw_batch)
                    skus = batch_payload.get("skus") if isinstance(batch_payload, dict) else None
                    if isinstance(skus, list) and len(skus) == batch.sku_count:
                        for item in items:
                            if item.item_index >= len(skus):
                                continue
                            sku = skus[item.item_index]
                            if (
                                isinstance(sku, str)
                                and hashlib.sha256(sku.encode("utf-8")).hexdigest() == item.sku_hash
                            ):
                                projected_skus[item.item_index] = sku
            except (PayloadAuthorizationError, ValueError, json.JSONDecodeError):
                projected_skus = {}
            projected_results: dict[UUID, dict[str, object]] = {}
            for item in items:
                if (
                    not item.result_ref
                    or not item.result_hash
                    or item.status not in {"SUCCEEDED", "BLOCKED"}
                ):
                    continue
                try:
                    raw = await self._payload_store.get_authorized(
                        tenant_id=tenant_id,
                        payload_ref=item.result_ref,
                        permission_scope=_PAYLOAD_PERMISSION,
                    )
                    if hashlib.sha256(raw).hexdigest() != item.result_hash:
                        continue
                    terminal = json.loads(raw)
                    if isinstance(terminal, dict):
                        projected_results[item.id] = public_result(validated_result(terminal))
                except (
                    PayloadAuthorizationError,
                    OutputContractError,
                    ValueError,
                    json.JSONDecodeError,
                ):
                    continue
            succeeded_count = sum(item.status == "SUCCEEDED" for item in items)
            blocked_count = sum(item.status == "BLOCKED" for item in items)
            return {
                "schema_version": "supply-chain.analysis-batch.v2",
                "batch_id": str(batch.id),
                "status": batch.status,
                "sku_count": batch.sku_count,
                "completed_count": batch.completed_count,
                "succeeded_count": succeeded_count,
                "blocked_count": blocked_count,
                "failed_count": batch.failed_count,
                "runtime_mode": batch.runtime_mode,
                "eta": {
                    "low_seconds": batch.eta_low_seconds,
                    "high_seconds": batch.eta_high_seconds,
                    "profile_version": batch.eta_profile_version,
                    "dynamic": batch.eta_ewma_seconds is not None,
                },
                "items": [
                    {
                        "item_id": str(item.id),
                        "index": item.item_index,
                        "sku": projected_skus.get(item.item_index, ""),
                        "status": item.status,
                        "execution_id": (
                            str(item.runtime_execution_id)
                            if item.runtime_execution_id is not None
                            else None
                        ),
                        "sequence": item.runtime_sequence,
                        "error": (
                            {
                                "phase": item.error_phase,
                                "category": item.error_category,
                                "retryable": item.error_retryable,
                                "safe_message": item.safe_message,
                                "request_id": item.request_id,
                            }
                            if item.safe_message is not None
                            else None
                        ),
                        "result": projected_results.get(item.id),
                    }
                    for item in items
                ],
            }

    async def list_activities(
        self,
        *,
        tenant_id: str,
        batch_id: UUID,
        after_stream_sequence: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(BatchActivity)
                    .where(
                        BatchActivity.tenant_id == tenant_id,
                        BatchActivity.batch_id == batch_id,
                        BatchActivity.stream_sequence > after_stream_sequence,
                    )
                    .order_by(BatchActivity.stream_sequence)
                    .limit(limit)
                )
            ).all()
        return [
            {
                "id": str(row.id),
                "stream_sequence": row.stream_sequence,
                "schema_version": "business-agent.activity-event.v1",
                "event_type": row.event_type,
                "phase": row.phase,
                "state": row.state,
                "message": row.safe_message,
                "execution_id": str(row.execution_id) if row.execution_id else None,
                "sequence": row.execution_sequence,
                "tool_name": row.tool_name,
                "duration_ms": row.duration_ms,
                "progress": {
                    "current": row.progress_current,
                    "total": row.progress_total,
                },
                "payload": dict(row.payload),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    async def active_executions(self) -> list[tuple[str, UUID, UUID, int]]:
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(
                        BatchItem.tenant_id,
                        BatchItem.batch_id,
                        BatchItem.runtime_execution_id,
                        BatchItem.runtime_sequence,
                    ).where(
                        BatchItem.status.in_(("DISPATCHED", "RUNNING", "WAITING")),
                        BatchItem.runtime_execution_id.is_not(None),
                    )
                )
            ).all()
        return [
            (row.tenant_id, row.batch_id, row.runtime_execution_id, row.runtime_sequence)
            for row in rows
            if row.runtime_execution_id is not None
        ]

    async def prune_retention(
        self,
        *,
        now: datetime,
        completed_activity_days: int,
        batch_mapping_days: int,
    ) -> tuple[int, int]:
        """Delete only terminal BFF projections after their versioned retention window."""

        terminal = ("SUCCEEDED", "BLOCKED", "PARTIAL", "FAILED", "CANCELLED")
        activity_cutoff = now - timedelta(days=completed_activity_days)
        batch_cutoff = now - timedelta(days=batch_mapping_days)
        async with self._factory() as session, session.begin():
            old_batch_ids = select(Batch.id).where(
                Batch.status.in_(terminal),
                Batch.completed_at.is_not(None),
                Batch.completed_at < activity_cutoff,
            )
            activity_result = await session.execute(
                delete(BatchActivity).where(BatchActivity.batch_id.in_(old_batch_ids))
            )
            batch_result = await session.execute(
                delete(Batch).where(
                    Batch.status.in_(terminal),
                    Batch.completed_at.is_not(None),
                    Batch.completed_at < batch_cutoff,
                )
            )
            return (
                int(cast(CursorResult[Any], activity_result).rowcount or 0),
                int(cast(CursorResult[Any], batch_result).rowcount or 0),
            )

    async def active_execution_targets(self) -> list[ActiveExecution]:
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(BatchItem).where(
                        BatchItem.status.in_(("DISPATCHED", "RUNNING", "WAITING")),
                        BatchItem.runtime_execution_id.is_not(None),
                    )
                )
            ).all()
        targets: list[ActiveExecution] = []
        for item in rows:
            if item.runtime_execution_id is not None:
                targets.append(
                    ActiveExecution(
                        tenant_id=item.tenant_id,
                        batch_id=item.batch_id,
                        execution_id=item.runtime_execution_id,
                        sequence=item.runtime_sequence,
                    )
                )
        return targets

    async def cancel_batch(self, *, tenant_id: str, batch_id: UUID) -> list[ActiveExecution] | None:
        targets = [
            target
            for target in await self.active_execution_targets()
            if target.tenant_id == tenant_id and target.batch_id == batch_id
        ]
        async with self._factory() as session, session.begin():
            batch = await session.scalar(
                select(Batch)
                .where(Batch.tenant_id == tenant_id, Batch.id == batch_id)
                .with_for_update()
            )
            if batch is None:
                return None
            batch.cancel_requested = True
            pending = (
                await session.scalars(
                    select(BatchItem)
                    .where(
                        BatchItem.tenant_id == tenant_id,
                        BatchItem.batch_id == batch_id,
                        BatchItem.status.in_(("PENDING", "DISPATCHING")),
                    )
                    .with_for_update()
                )
            ).all()
            now = datetime.now(UTC)
            for item in pending:
                item.status = "CANCELLED"
                item.completed_at = now
                item.lease_owner = None
                item.lease_expires_at = None
            await self._insert_activity(
                session,
                tenant_id=tenant_id,
                batch_id=batch_id,
                item_id=None,
                execution_id=None,
                event_key="batch:cancel-requested",
                event_type="batch.cancel-requested",
                phase="execution",
                state="cancelled",
                safe_message="Cancellation requested.",
                critical=True,
            )
            await self._refresh_batch(session, tenant_id=tenant_id, batch_id=batch_id)
        return targets

    async def _owned_item(self, session: AsyncSession, claim: ClaimedItem) -> BatchItem | None:
        return cast(
            BatchItem | None,
            await session.scalar(
                select(BatchItem)
                .where(
                    BatchItem.tenant_id == claim.tenant_id,
                    BatchItem.id == claim.item_id,
                    BatchItem.status == "DISPATCHING",
                    BatchItem.lease_owner == claim.lease_owner,
                )
                .with_for_update()
            ),
        )

    @staticmethod
    async def _batch(session: AsyncSession, tenant_id: str, batch_id: UUID) -> Batch:
        batch = await session.scalar(
            select(Batch)
            .where(Batch.tenant_id == tenant_id, Batch.id == batch_id)
            .with_for_update()
        )
        if batch is None:
            raise RuntimeError("batch disappeared during dispatch")
        return batch

    async def _refresh_batch(
        self, session: AsyncSession, *, tenant_id: str, batch_id: UUID
    ) -> None:
        batch = await self._batch(session, tenant_id, batch_id)
        statuses = (
            await session.execute(
                select(BatchItem.status, func.count().label("item_count"))
                .where(BatchItem.tenant_id == tenant_id, BatchItem.batch_id == batch_id)
                .group_by(BatchItem.status)
            )
        ).all()
        counts = {row.status: int(row.item_count) for row in statuses}
        completed = sum(counts.get(status, 0) for status in _TERMINAL_ITEM_STATUSES)
        failed = counts.get("FAILED", 0)
        batch.completed_count = completed
        batch.failed_count = failed
        if completed == batch.sku_count:
            if counts.get("CANCELLED", 0) == batch.sku_count:
                batch.status = "CANCELLED"
            elif failed == batch.sku_count:
                batch.status = "FAILED"
            elif counts.get("BLOCKED", 0) == batch.sku_count:
                batch.status = "BLOCKED"
            elif failed or counts.get("BLOCKED", 0):
                batch.status = "PARTIAL"
            else:
                batch.status = "SUCCEEDED"
            batch.completed_at = datetime.now(UTC)
        elif counts.get("WAITING", 0) and not any(
            counts.get(status, 0) for status in ("PENDING", "DISPATCHING", "DISPATCHED", "RUNNING")
        ):
            batch.status = "WAITING"
        else:
            batch.status = "RUNNING"
        await self._refresh_eta(session, batch=batch, counts=counts)

    async def _refresh_eta(
        self,
        session: AsyncSession,
        *,
        batch: Batch,
        counts: dict[str, int],
    ) -> None:
        if counts.get("WAITING", 0) or batch.completed_count < 2:
            return
        now = datetime.now(UTC)
        if batch.eta_updated_at is not None and now - batch.eta_updated_at < timedelta(seconds=5):
            return
        completed_items = (
            await session.scalars(
                select(BatchItem)
                .where(
                    BatchItem.tenant_id == batch.tenant_id,
                    BatchItem.batch_id == batch.id,
                    BatchItem.started_at.is_not(None),
                    BatchItem.completed_at.is_not(None),
                )
                .order_by(BatchItem.completed_at)
            )
        ).all()
        observations = [
            (item.completed_at - item.started_at).total_seconds()
            for item in completed_items
            if item.completed_at is not None and item.started_at is not None
        ]
        if len(observations) < 2:
            return
        ewma = observations[0]
        for duration in observations[1:]:
            ewma = 0.3 * duration + 0.7 * ewma
        estimator = EtaEstimator(self._eta_profile)
        estimator.restore_ewma(ewma)
        estimate = estimator.estimate(
            item_count=batch.sku_count,
            completed_count=batch.completed_count,
        )
        previous_center = (batch.eta_low_seconds + batch.eta_high_seconds) / 2
        next_center = (estimate.low_seconds + estimate.high_seconds) / 2
        if previous_center > 0 and abs(next_center - previous_center) / previous_center <= 0.1:
            return
        batch.eta_low_seconds = estimate.low_seconds
        batch.eta_high_seconds = estimate.high_seconds
        batch.eta_ewma_seconds = ewma
        batch.eta_updated_at = now
        await self._insert_activity(
            session,
            tenant_id=batch.tenant_id,
            batch_id=batch.id,
            item_id=None,
            execution_id=None,
            event_key=f"batch:eta:{int(now.timestamp() // 5)}",
            event_type="batch.eta-updated",
            phase="execution",
            state="progress",
            safe_message="Estimated completion time updated.",
            payload={
                "schema_version": "business-agent.activity-event.v1",
                "eta": {
                    "low_seconds": estimate.low_seconds,
                    "high_seconds": estimate.high_seconds,
                    "profile_version": estimate.profile_version,
                    "dynamic": True,
                },
            },
        )

    @staticmethod
    async def _insert_activity(
        session: AsyncSession,
        *,
        tenant_id: str,
        batch_id: UUID,
        item_id: UUID | None,
        execution_id: UUID | None,
        event_key: str,
        event_type: str,
        phase: str,
        state: str,
        safe_message: str,
        tool_name: str | None = None,
        duration_ms: int | None = None,
        payload: dict[str, object] | None = None,
        execution_sequence: int | None = None,
        critical: bool = False,
    ) -> None:
        await session.execute(
            pg_insert(BatchActivity)
            .values(
                tenant_id=tenant_id,
                batch_id=batch_id,
                item_id=item_id,
                execution_id=execution_id,
                execution_sequence=execution_sequence,
                event_key=event_key[:256],
                event_type=event_type[:128],
                phase=phase[:64],
                state=state[:32],
                safe_message=safe_message[:1024],
                tool_name=tool_name,
                duration_ms=duration_ms,
                payload=payload or {"schema_version": "business-agent.activity-event.v1"},
                critical=critical,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "batch_id", "event_key"])
        )
