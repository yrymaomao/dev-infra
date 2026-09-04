"""Transactional Level 2 repository for selection, policy, reports, schedules, and MQ."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_runtime.payloads.contracts import PayloadAuthorizationError, PayloadStore
from pydantic import JsonValue
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .activity import ActivityProjection
from .batch_result_contract import validated_batch_artifact, validated_batch_output
from .level2_contracts import (
    CsvSelectionRow,
    InventorySelector,
    ReportRunRequest,
    ScheduleCreate,
    SchedulePatch,
)
from .level2_models import (
    PolicyVersion,
    ReportActivity,
    ReportBatch,
    ReportInbox,
    ReportOutbox,
    ReportRun,
    ReportSchedule,
    SelectionPreview,
)
from .policy import default_policy_document, validate_policy
from .report_mq import ReportBatchMessage
from .runtime_client import RuntimeStartResult
from .selection_csv import CsvSelection

_PAYLOAD_PERMISSION = "supply-chain:level2"
_TERMINAL_REPORT = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"})


class ResourceConflict(RuntimeError):
    pass


class ResourceNotReady(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CreatedReport:
    report_run_id: UUID
    sku_count: int
    batch_count: int


@dataclass(frozen=True, slots=True)
class ClaimedOutbox:
    event_id: UUID
    tenant_id: str
    payload: dict[str, object]
    lease_owner: str


@dataclass(frozen=True, slots=True)
class ClaimedPreview:
    preview_id: UUID
    tenant_id: str
    source_kind: str
    selector: dict[str, object]
    payload_ref: str
    payload_hash: str
    lease_owner: str
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedReportBatch:
    event_id: UUID
    tenant_id: str
    report_run_id: UUID
    batch_id: UUID
    batch_no: int
    item_offset: int
    item_count: int
    selection_payload_ref: str
    policy_snapshot_ref: str
    data_cutoff: datetime
    lease_owner: str


@dataclass(frozen=True, slots=True)
class ActiveReportExecution:
    tenant_id: str
    report_run_id: UUID
    batch_id: UUID
    execution_id: UUID
    sequence: int
    item_offset: int
    item_count: int


@dataclass(frozen=True, slots=True)
class ClaimedScheduledReport:
    tenant_id: str
    report_run_id: UUID
    schedule_id: UUID
    selection_preview_id: UUID | None
    lease_owner: str


@dataclass(frozen=True, slots=True)
class ResolvedPolicySnapshot:
    version: int | None
    document_ref: str
    risk_flags: tuple[str, ...] = ()


class Level2Repository:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        payload_store: PayloadStore,
        preview_ttl: timedelta = timedelta(hours=1),
    ) -> None:
        self._factory = factory
        self._payload_store = payload_store
        self._preview_ttl = preview_ttl

    async def create_preview(
        self,
        *,
        tenant_id: str,
        client_request_id: str,
        source_kind: str,
        request_payload: dict[str, object],
        selector: dict[str, object] | None,
        now: datetime,
    ) -> UUID:
        staged = await self._stage(tenant_id, request_payload)
        preview_id = uuid4()
        try:
            async with self._factory() as session, session.begin():
                session.add(
                    SelectionPreview(
                        id=preview_id,
                        tenant_id=tenant_id,
                        client_request_id=client_request_id,
                        source_kind=source_kind,
                        status="PLANNING" if source_kind == "NATURAL_LANGUAGE" else "DISCOVERING",
                        selector=selector or {},
                        payload_ref=staged.payload_ref,
                        payload_hash=staged.payload_hash,
                        next_attempt_at=now,
                        expires_at=now + self._preview_ttl,
                    )
                )
        except IntegrityError:
            async with self._factory() as session:
                existing = await session.scalar(
                    select(SelectionPreview).where(
                        SelectionPreview.tenant_id == tenant_id,
                        SelectionPreview.client_request_id == client_request_id,
                    )
                )
            if existing is None:
                raise
            existing_payload = await self._load(
                tenant_id,
                existing.payload_ref,
                existing.payload_hash,
            )
            if existing_payload != request_payload:
                raise ResourceConflict("client_request_id was already used for another selection")
            return existing.id
        await self._commit_staged(tenant_id, staged)
        return preview_id

    async def create_csv_preview(
        self,
        *,
        tenant_id: str,
        client_request_id: str,
        parsed: CsvSelection,
        now: datetime,
    ) -> UUID:
        rows_payload = {"rows": [row.model_dump(mode="json") for row in parsed.rows]}
        errors_payload = {"errors": [error.model_dump(mode="json") for error in parsed.errors]}
        staged = await self._stage(tenant_id, rows_payload)
        staged_errors = await self._stage(tenant_id, errors_payload)
        preview_id = uuid4()
        snapshot_id = hashlib.sha256(
            json.dumps(rows_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        try:
            async with self._factory() as session, session.begin():
                session.add(
                    SelectionPreview(
                        id=preview_id,
                        tenant_id=tenant_id,
                        client_request_id=client_request_id,
                        source_kind="CSV",
                        status="READY" if parsed.rows else "REJECTED",
                        selector={},
                        source_snapshot_id=f"csv:{snapshot_id}",
                        snapshot_time=now,
                        matched_count=len(parsed.rows),
                        payload_ref=staged.payload_ref,
                        payload_hash=staged.payload_hash,
                        row_errors_ref=staged_errors.payload_ref,
                        warnings=(
                            [f"{len(parsed.errors)} CSV row(s) were excluded."]
                            if parsed.errors
                            else []
                        ),
                        ambiguities=[],
                        next_attempt_at=now,
                        expires_at=now + self._preview_ttl,
                    )
                )
        except IntegrityError:
            async with self._factory() as session:
                existing = await session.scalar(
                    select(SelectionPreview).where(
                        SelectionPreview.tenant_id == tenant_id,
                        SelectionPreview.client_request_id == client_request_id,
                    )
                )
            if existing is None or existing.payload_hash != staged.payload_hash:
                raise ResourceConflict("client_request_id was already used for another CSV")
            return existing.id
        await self._commit_staged(tenant_id, staged)
        await self._commit_staged(tenant_id, staged_errors)
        return preview_id

    async def claim_preview(self, *, worker_id: str, now: datetime) -> ClaimedPreview | None:
        async with self._factory() as session, session.begin():
            preview = await session.scalar(
                select(SelectionPreview)
                .where(
                    SelectionPreview.status.in_(("PLANNING", "DISCOVERING", "PROCESSING")),
                    SelectionPreview.next_attempt_at <= now,
                    SelectionPreview.expires_at > now,
                    SelectionPreview.confirmed.is_(False),
                    or_(
                        SelectionPreview.lease_expires_at.is_(None),
                        SelectionPreview.lease_expires_at <= func.current_timestamp(),
                    ),
                )
                .order_by(SelectionPreview.created_at, SelectionPreview.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if preview is None or preview.payload_ref is None or preview.payload_hash is None:
                return None
            preview.status = "PROCESSING"
            preview.attempt_count += 1
            preview.lease_owner = worker_id
            preview.lease_expires_at = now + timedelta(minutes=5)
            return ClaimedPreview(
                preview_id=preview.id,
                tenant_id=preview.tenant_id,
                source_kind=preview.source_kind,
                selector=dict(preview.selector),
                payload_ref=preview.payload_ref,
                payload_hash=preview.payload_hash,
                lease_owner=worker_id,
                requested_at=preview.created_at,
            )

    async def load_preview_request(self, claim: ClaimedPreview) -> dict[str, Any]:
        return await self._load(claim.tenant_id, claim.payload_ref, claim.payload_hash)

    async def retry_preview(
        self,
        claim: ClaimedPreview,
        *,
        now: datetime,
        safe_warning: str,
        terminal: bool = False,
    ) -> None:
        async with self._factory() as session, session.begin():
            preview = await session.scalar(
                select(SelectionPreview)
                .where(
                    SelectionPreview.tenant_id == claim.tenant_id,
                    SelectionPreview.id == claim.preview_id,
                    SelectionPreview.status == "PROCESSING",
                    SelectionPreview.lease_owner == claim.lease_owner,
                )
                .with_for_update()
            )
            if preview is None:
                return
            preview.warnings = [*preview.warnings, safe_warning[:1024]]
            preview.lease_owner = None
            preview.lease_expires_at = None
            if terminal or preview.attempt_count >= 5:
                preview.status = "REJECTED"
            else:
                preview.status = (
                    "PLANNING" if preview.source_kind == "NATURAL_LANGUAGE" else "DISCOVERING"
                )
                preview.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(preview.attempt_count, 8))
                )

    async def complete_preview(
        self,
        *,
        tenant_id: str,
        preview_id: UUID,
        lease_owner: str,
        selector: dict[str, object],
        source_snapshot_id: str,
        snapshot_time: datetime,
        rows: list[dict[str, object]],
        warnings: list[str] | None = None,
        ambiguities: list[str] | None = None,
    ) -> None:
        if len(rows) > 10_001:
            raise ValueError("selection discovery must stop after the 10001st match")
        skus = [row.get("sku") for row in rows]
        if not all(isinstance(sku, str) and sku for sku in skus):
            raise ValueError("selection rows require canonical SKU")
        sku_values = cast(list[str], skus)
        if sku_values != sorted(sku_values) or len(set(sku_values)) != len(sku_values):
            raise ValueError("selection rows must be unique and ordered by SKU_ASC")
        staged = await self._stage(tenant_id, {"rows": rows})
        async with self._factory() as session, session.begin():
            preview = await session.scalar(
                select(SelectionPreview)
                .where(
                    SelectionPreview.tenant_id == tenant_id,
                    SelectionPreview.id == preview_id,
                    SelectionPreview.status == "PROCESSING",
                    SelectionPreview.lease_owner == lease_owner,
                )
                .with_for_update()
            )
            if preview is None:
                raise ResourceConflict("selection preview lease is no longer owned")
            if preview.confirmed:
                raise ResourceConflict("confirmed selection preview is immutable")
            preview.selector = selector
            preview.source_snapshot_id = source_snapshot_id
            preview.snapshot_time = snapshot_time
            preview.matched_count = len(rows)
            preview.payload_ref = staged.payload_ref
            preview.payload_hash = staged.payload_hash
            preview.warnings = list(warnings or [])
            preview.ambiguities = list(ambiguities or [])
            preview.status = "READY" if 0 < len(rows) <= 10_000 else "REJECTED"
            preview.lease_owner = None
            preview.lease_expires_at = None
            if len(rows) > 10_000:
                preview.warnings = [*preview.warnings, "NARROWING_REQUIRED"]
        await self._commit_staged(tenant_id, staged)

    async def get_preview(
        self, *, tenant_id: str, preview_id: UUID, now: datetime
    ) -> dict[str, Any] | None:
        async with self._factory() as session:
            preview = await session.scalar(
                select(SelectionPreview).where(
                    SelectionPreview.tenant_id == tenant_id,
                    SelectionPreview.id == preview_id,
                )
            )
        if preview is None:
            return None
        status = (
            "EXPIRED" if not preview.confirmed and preview.expires_at <= now else preview.status
        )
        payload = await self._load(tenant_id, preview.payload_ref, preview.payload_hash)
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        errors: list[object] = []
        if preview.row_errors_ref:
            error_payload = await self._load(tenant_id, preview.row_errors_ref, None)
            if isinstance(error_payload, dict) and isinstance(error_payload.get("errors"), list):
                errors = error_payload["errors"]
        return {
            "schema_version": "supply-chain.selection-preview.v1",
            "preview_id": str(preview.id),
            "status": status,
            "selector": dict(preview.selector),
            "source_snapshot_id": preview.source_snapshot_id,
            "snapshot_time": preview.snapshot_time.isoformat() if preview.snapshot_time else None,
            "matched_count": preview.matched_count,
            "sample_skus": [row["sku"] for row in rows[:20] if isinstance(row, dict)],
            "warnings": list(preview.warnings),
            "ambiguities": list(preview.ambiguities),
            "expires_at": preview.expires_at.isoformat(),
            "confirmed": preview.confirmed,
            "row_errors": errors,
        }

    async def publish_policy(
        self,
        *,
        tenant_id: str,
        document: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        digest, warnings = validate_policy(document)
        staged = await self._stage(tenant_id, cast(dict[str, object], document))
        async with self._factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:tenant_id))"),
                {"tenant_id": f"supply-chain-policy:{tenant_id}"},
            )
            existing = await session.scalar(
                select(PolicyVersion).where(
                    PolicyVersion.tenant_id == tenant_id,
                    PolicyVersion.digest == digest,
                )
            )
            if existing is not None:
                return await self._public_policy(existing, document)
            current = int(
                await session.scalar(
                    select(func.max(PolicyVersion.version)).where(
                        PolicyVersion.tenant_id == tenant_id
                    )
                )
                or 0
            )
            policy = PolicyVersion(
                tenant_id=tenant_id,
                version=current + 1,
                digest=digest,
                document_ref=cast(str, staged.payload_ref),
                active=False,
                warnings=list(warnings),
                published_at=now,
            )
            session.add(policy)
        await self._commit_staged(tenant_id, staged)
        return await self._public_policy(policy, document)

    async def activate_policy(
        self, *, tenant_id: str, version: int, now: datetime
    ) -> dict[str, Any] | None:
        async with self._factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:tenant_id))"),
                {"tenant_id": f"supply-chain-policy:{tenant_id}"},
            )
            policy = await session.scalar(
                select(PolicyVersion)
                .where(PolicyVersion.tenant_id == tenant_id, PolicyVersion.version == version)
                .with_for_update()
            )
            if policy is None:
                return None
            await session.execute(
                update(PolicyVersion)
                .where(PolicyVersion.tenant_id == tenant_id, PolicyVersion.active.is_(True))
                .values(active=False, activated_at=None)
            )
            policy.active = True
            policy.activated_at = now
        document = await self._load(tenant_id, policy.document_ref, None)
        return await self._public_policy(policy, document)

    async def get_policy(self, *, tenant_id: str, version: int) -> dict[str, Any] | None:
        async with self._factory() as session:
            policy = await session.scalar(
                select(PolicyVersion).where(
                    PolicyVersion.tenant_id == tenant_id,
                    PolicyVersion.version == version,
                )
            )
        if policy is None:
            return None
        return await self._public_policy(
            policy,
            await self._load(tenant_id, policy.document_ref, None),
        )

    async def list_policies(self, *, tenant_id: str) -> list[dict[str, Any]]:
        async with self._factory() as session:
            policies = (
                await session.scalars(
                    select(PolicyVersion)
                    .where(PolicyVersion.tenant_id == tenant_id)
                    .order_by(PolicyVersion.version.desc())
                )
            ).all()
        result: list[dict[str, Any]] = []
        for policy in policies:
            result.append(
                await self._public_policy(
                    policy,
                    await self._load(tenant_id, policy.document_ref, None),
                )
            )
        return result

    async def create_report(
        self,
        *,
        tenant_id: str,
        request: ReportRunRequest,
        now: datetime,
        trace_id: str,
    ) -> CreatedReport:
        async with self._factory() as session:
            existing = await session.scalar(
                select(ReportRun).where(
                    ReportRun.tenant_id == tenant_id,
                    ReportRun.client_request_id == request.client_request_id,
                )
            )
            if existing is not None:
                if existing.selection_preview_id != request.selection_preview_id:
                    raise ResourceConflict("client_request_id was already used for another report")
                return CreatedReport(existing.id, existing.sku_count, existing.batch_count)
            preview = await session.scalar(
                select(SelectionPreview).where(
                    SelectionPreview.tenant_id == tenant_id,
                    SelectionPreview.id == request.selection_preview_id,
                )
            )
        policy = await self._resolve_policy_snapshot(
            tenant_id=tenant_id,
            policy_mode=request.policy_mode,
            policy_version=request.policy_version,
        )
        if preview is None:
            raise LookupError("selection preview not found")
        if preview.status != "READY" or preview.expires_at <= now or preview.confirmed:
            raise ResourceNotReady("selection preview is not ready for confirmation")
        if preview.snapshot_time is None:
            raise ResourceNotReady("selection preview has no stable data cutoff")
        if not 1 <= preview.matched_count <= 10_000:
            raise ResourceNotReady("selection must contain between 1 and 10000 SKU")
        if policy is None:
            raise ResourceNotReady("an active or pinned policy is required")
        batch_count = (preview.matched_count + 199) // 200
        report_id = uuid4()
        report_batches: list[ReportBatch] = []
        outbox: list[ReportOutbox] = []
        for index in range(batch_count):
            batch_id = uuid4()
            item_count = min(200, preview.matched_count - index * 200)
            report_batches.append(
                ReportBatch(
                    id=batch_id,
                    tenant_id=tenant_id,
                    report_run_id=report_id,
                    batch_no=index + 1,
                    item_offset=index * 200,
                    item_count=item_count,
                    status="QUEUED",
                    next_attempt_at=now,
                )
            )
            event_id = uuid4()
            outbox.append(
                ReportOutbox(
                    event_id=event_id,
                    tenant_id=tenant_id,
                    report_run_id=report_id,
                    batch_id=batch_id,
                    event_type="supply-chain.report-batch.requested.v1",
                    payload={
                        "event_id": str(event_id),
                        "schema_version": 1,
                        "report_run_id": str(report_id),
                        "batch_id": str(batch_id),
                        "selection_snapshot_id": str(preview.id),
                        "batch_no": index + 1,
                        "item_count": item_count,
                        "policy_snapshot_ref": policy.document_ref,
                        "trace_id": trace_id,
                        "occurred_at": now.isoformat(),
                    },
                    status="PENDING",
                    next_attempt_at=now,
                )
            )
        try:
            async with self._factory() as session, session.begin():
                locked_preview = await session.scalar(
                    select(SelectionPreview)
                    .where(
                        SelectionPreview.tenant_id == tenant_id,
                        SelectionPreview.id == preview.id,
                        SelectionPreview.confirmed.is_(False),
                    )
                    .with_for_update()
                )
                if locked_preview is None:
                    raise ResourceConflict("selection preview was already confirmed")
                locked_preview.confirmed = True
                session.add(
                    ReportRun(
                        id=report_id,
                        tenant_id=tenant_id,
                        client_request_id=request.client_request_id,
                        selection_preview_id=preview.id,
                        policy_mode=request.policy_mode,
                        policy_version=policy.version,
                        policy_snapshot_ref=policy.document_ref,
                        data_cutoff=preview.snapshot_time,
                        status="ACCEPTED",
                        sku_count=preview.matched_count,
                        batch_count=batch_count,
                        risk_flags=list(policy.risk_flags),
                        next_attempt_at=now,
                    )
                )
                # The ORM models intentionally have no bidirectional relationships. Flush
                # each durable ownership level so PostgreSQL foreign keys establish the
                # same report -> batch -> outbox ordering on every supported SQLAlchemy
                # version.
                await session.flush()
                session.add_all(report_batches)
                await session.flush()
                session.add_all(outbox)
                session.add(
                    ReportActivity(
                        tenant_id=tenant_id,
                        report_run_id=report_id,
                        event_key="report:accepted",
                        event_type="report.accepted",
                        phase="selection",
                        state="accepted",
                        safe_message=(
                            f"Report accepted with {preview.matched_count} SKU(s) "
                            f"in {batch_count} batch(es)."
                        ),
                        progress_current=0,
                        progress_total=batch_count,
                        payload={"schema_version": "business-agent.activity-event.v1"},
                        critical=True,
                    )
                )
        except IntegrityError:
            async with self._factory() as session:
                existing = await session.scalar(
                    select(ReportRun).where(
                        ReportRun.tenant_id == tenant_id,
                        ReportRun.client_request_id == request.client_request_id,
                    )
                )
            if existing is None:
                raise
            if existing.selection_preview_id != request.selection_preview_id:
                raise ResourceConflict("client_request_id was already used for another report")
            return CreatedReport(existing.id, existing.sku_count, existing.batch_count)
        return CreatedReport(report_id, preview.matched_count, batch_count)

    async def get_report(
        self,
        *,
        tenant_id: str,
        report_run_id: UUID,
        item_offset: int = 0,
        item_limit: int = 200,
    ) -> dict[str, Any] | None:
        async with self._factory() as session:
            report = await session.scalar(
                select(ReportRun).where(
                    ReportRun.tenant_id == tenant_id,
                    ReportRun.id == report_run_id,
                )
            )
            if report is None:
                return None
            batches = (
                await session.scalars(
                    select(ReportBatch)
                    .where(
                        ReportBatch.tenant_id == tenant_id,
                        ReportBatch.report_run_id == report_run_id,
                    )
                    .order_by(ReportBatch.batch_no)
                )
            ).all()
        end_offset = min(report.sku_count, item_offset + item_limit)
        items: list[dict[str, Any]] = []
        for batch in batches:
            if (
                batch.result_ref is None
                or batch.result_hash is None
                or batch.item_offset >= end_offset
                or batch.item_offset + batch.item_count <= item_offset
            ):
                continue
            artifact = await self._load(tenant_id, batch.result_ref, batch.result_hash)
            validated = validated_batch_artifact(
                artifact,
                report_run_id=report.id,
                batch_id=batch.id,
                item_offset=batch.item_offset,
                expected_item_count=batch.item_count,
                expected_counts=(
                    batch.complete_count,
                    batch.blocked_count,
                    batch.failed_count,
                ),
            )
            items.extend(
                cast(dict[str, Any], item)
                for item in cast(list[object], validated["items"])
                if isinstance(item, dict) and item_offset <= cast(int, item["ordinal"]) < end_offset
            )
        items.sort(key=lambda item: cast(int, item["ordinal"]))
        return {
            "schema_version": "supply-chain.report.v1",
            "report_run_id": str(report.id),
            "status": report.status,
            "sku_count": report.sku_count,
            "batch_count": report.batch_count,
            "complete_count": report.complete_count,
            "blocked_count": report.blocked_count,
            "failed_count": report.failed_count,
            "risk_flags": list(report.risk_flags),
            "data_cutoff": report.data_cutoff.isoformat() if report.data_cutoff else None,
            "item_offset": item_offset,
            "item_limit": item_limit,
            "returned_count": len(items),
            "next_item_offset": end_offset if end_offset < report.sku_count else None,
            "items": items,
            "batches": [
                {
                    "batch_id": str(batch.id),
                    "batch_no": batch.batch_no,
                    "item_count": batch.item_count,
                    "status": batch.status,
                    "complete_count": batch.complete_count,
                    "blocked_count": batch.blocked_count,
                    "failed_count": batch.failed_count,
                    "safe_error": batch.safe_error,
                    "execution_id": (
                        str(batch.runtime_execution_id)
                        if batch.runtime_execution_id is not None
                        else None
                    ),
                }
                for batch in batches
            ],
        }

    async def list_reports(self, *, tenant_id: str, limit: int) -> list[dict[str, Any]]:
        async with self._factory() as session:
            reports = (
                await session.scalars(
                    select(ReportRun)
                    .where(ReportRun.tenant_id == tenant_id)
                    .order_by(ReportRun.created_at.desc(), ReportRun.id.desc())
                    .limit(limit)
                )
            ).all()
        return [
            {
                "report_run_id": str(report.id),
                "status": report.status,
                "sku_count": report.sku_count,
                "batch_count": report.batch_count,
                "complete_count": report.complete_count,
                "blocked_count": report.blocked_count,
                "failed_count": report.failed_count,
                "risk_flags": list(report.risk_flags),
                "data_cutoff": report.data_cutoff.isoformat() if report.data_cutoff else None,
                "created_at": report.created_at.isoformat(),
                "completed_at": (
                    report.completed_at.isoformat() if report.completed_at is not None else None
                ),
            }
            for report in reports
        ]

    async def get_report_status(self, *, tenant_id: str, report_run_id: UUID) -> str | None:
        async with self._factory() as session:
            return cast(
                str | None,
                await session.scalar(
                    select(ReportRun.status).where(
                        ReportRun.tenant_id == tenant_id,
                        ReportRun.id == report_run_id,
                    )
                ),
            )

    async def cancel_report(self, *, tenant_id: str, report_run_id: UUID) -> bool:
        async with self._factory() as session, session.begin():
            report = await session.scalar(
                select(ReportRun)
                .where(ReportRun.tenant_id == tenant_id, ReportRun.id == report_run_id)
                .with_for_update()
            )
            if report is None:
                return False
            if report.status in _TERMINAL_REPORT:
                return True
            report.cancel_requested = True
            await session.execute(
                update(ReportBatch)
                .where(
                    ReportBatch.tenant_id == tenant_id,
                    ReportBatch.report_run_id == report_run_id,
                    ReportBatch.status == "QUEUED",
                )
                .values(status="CANCELLED", completed_at=func.current_timestamp())
            )
            await session.execute(
                update(ReportOutbox)
                .where(
                    ReportOutbox.tenant_id == tenant_id,
                    ReportOutbox.report_run_id == report_run_id,
                    ReportOutbox.status == "PENDING",
                )
                .values(status="CANCELLED", lease_owner=None, lease_expires_at=None)
            )
            report.status = "CANCELLED"
            report.completed_at = datetime.now(UTC)
            session.add(
                ReportActivity(
                    tenant_id=tenant_id,
                    report_run_id=report_run_id,
                    event_key="report:cancelled",
                    event_type="report.cancelled",
                    phase="execution",
                    state="cancelled",
                    safe_message="Report cancellation requested.",
                    payload={"schema_version": "business-agent.activity-event.v1"},
                    critical=True,
                )
            )
        return True

    async def list_report_activities(
        self,
        *,
        tenant_id: str,
        report_run_id: UUID,
        after_sequence: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(ReportActivity)
                    .where(
                        ReportActivity.tenant_id == tenant_id,
                        ReportActivity.report_run_id == report_run_id,
                        ReportActivity.stream_sequence > after_sequence,
                    )
                    .order_by(ReportActivity.stream_sequence)
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
                "batch_id": str(row.batch_id) if row.batch_id else None,
                "progress": {"current": row.progress_current, "total": row.progress_total},
                "payload": dict(row.payload),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    async def record_report_runtime_event(
        self,
        *,
        target: ActiveReportExecution,
        projection: ActivityProjection,
    ) -> None:
        if projection.execution_id != str(target.execution_id):
            raise ValueError("Runtime activity execution does not match the report batch")
        if projection.execution_sequence < 1:
            raise ValueError("Runtime activity sequence must be positive")
        async with self._factory() as session, session.begin():
            batch = await session.scalar(
                select(ReportBatch)
                .where(
                    ReportBatch.tenant_id == target.tenant_id,
                    ReportBatch.report_run_id == target.report_run_id,
                    ReportBatch.id == target.batch_id,
                    ReportBatch.runtime_execution_id == target.execution_id,
                )
                .with_for_update()
            )
            if batch is None or projection.execution_sequence <= batch.runtime_sequence:
                return
            batch.runtime_sequence = projection.execution_sequence
            if batch.status not in {"SUCCEEDED", "BLOCKED", "PARTIAL", "FAILED", "CANCELLED"}:
                batch.status = "WAITING" if projection.state == "waiting" else "RUNNING"
            payload = dict(projection.payload)
            payload.update(
                {
                    "execution_id": projection.execution_id,
                    "sequence": projection.execution_sequence,
                    "tool_name": projection.tool_name,
                    "duration_ms": projection.duration_ms,
                }
            )
            session.add(
                ReportActivity(
                    tenant_id=target.tenant_id,
                    report_run_id=target.report_run_id,
                    batch_id=target.batch_id,
                    event_key=projection.event_key,
                    event_type=projection.event_type,
                    phase=projection.phase,
                    state=projection.state,
                    safe_message=projection.safe_message,
                    payload=payload,
                    critical=projection.critical,
                )
            )

    async def claim_outbox(self, *, worker_id: str, now: datetime) -> ClaimedOutbox | None:
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(ReportOutbox)
                .where(
                    ReportOutbox.status.in_(("PENDING", "PUBLISHING")),
                    ReportOutbox.next_attempt_at <= now,
                    or_(
                        ReportOutbox.lease_expires_at.is_(None),
                        ReportOutbox.lease_expires_at <= func.current_timestamp(),
                    ),
                )
                .order_by(ReportOutbox.created_at, ReportOutbox.event_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            row.status = "PUBLISHING"
            row.attempt_count += 1
            row.lease_owner = worker_id
            row.lease_expires_at = now + timedelta(seconds=30)
            return ClaimedOutbox(row.event_id, row.tenant_id, dict(row.payload), worker_id)

    async def mark_outbox_published(self, claim: ClaimedOutbox, *, now: datetime) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(
                update(ReportOutbox)
                .where(
                    ReportOutbox.tenant_id == claim.tenant_id,
                    ReportOutbox.event_id == claim.event_id,
                    ReportOutbox.status == "PUBLISHING",
                    ReportOutbox.lease_owner == claim.lease_owner,
                )
                .values(
                    status="SENT",
                    published_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )

    async def mark_outbox_retry(self, claim: ClaimedOutbox, *, now: datetime) -> None:
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(ReportOutbox)
                .where(
                    ReportOutbox.tenant_id == claim.tenant_id,
                    ReportOutbox.event_id == claim.event_id,
                    ReportOutbox.status == "PUBLISHING",
                    ReportOutbox.lease_owner == claim.lease_owner,
                )
                .with_for_update()
            )
            if row is None:
                return
            delay = min(300, 2 ** min(row.attempt_count, 8))
            row.status = "PENDING"
            row.next_attempt_at = now + timedelta(seconds=delay)
            row.lease_owner = None
            row.lease_expires_at = None

    async def claim_report_delivery(
        self,
        *,
        message: ReportBatchMessage,
        worker_id: str,
        now: datetime,
        tenant_concurrency: int,
        global_concurrency: int,
    ) -> ClaimedReportBatch | None:
        event_id = UUID(message.event_id)
        batch_id = UUID(message.batch_id)
        report_run_id = UUID(message.report_run_id)
        async with self._factory() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(78974213601234017)"))
            already = await session.scalar(
                select(ReportInbox.event_id).where(ReportInbox.event_id == event_id)
            )
            if already is not None:
                return None
            batch = await session.scalar(
                select(ReportBatch)
                .where(
                    ReportBatch.id == batch_id,
                    ReportBatch.report_run_id == report_run_id,
                )
                .with_for_update()
            )
            if batch is None:
                raise ValueError("report batch message does not match a durable batch")
            report = await session.scalar(
                select(ReportRun)
                .where(
                    ReportRun.tenant_id == batch.tenant_id,
                    ReportRun.id == report_run_id,
                )
                .with_for_update()
            )
            if report is None:
                raise ValueError("report batch has no owning report")
            outbox = await session.scalar(
                select(ReportOutbox).where(
                    ReportOutbox.tenant_id == batch.tenant_id,
                    ReportOutbox.event_id == event_id,
                    ReportOutbox.batch_id == batch_id,
                    ReportOutbox.report_run_id == report_run_id,
                )
            )
            if outbox is None:
                raise ValueError("report batch message is not owned by the outbox")
            if (
                batch.batch_no != message.batch_no
                or batch.item_count != message.item_count
                or str(report.selection_preview_id) != message.selection_snapshot_id
                or report.policy_snapshot_ref != message.policy_snapshot_ref
            ):
                raise ValueError("report batch message contract does not match persisted state")
            if batch.status in {"SUCCEEDED", "BLOCKED", "PARTIAL", "FAILED", "CANCELLED"}:
                session.add(
                    ReportInbox(
                        event_id=event_id,
                        tenant_id=batch.tenant_id,
                        event_type="supply-chain.report-batch.requested.v1",
                    )
                )
                return None
            if report.cancel_requested:
                batch.status = "CANCELLED"
                batch.completed_at = now
                session.add(
                    ReportInbox(
                        event_id=event_id,
                        tenant_id=batch.tenant_id,
                        event_type="supply-chain.report-batch.requested.v1",
                    )
                )
                return None
            global_active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ReportBatch)
                    .where(
                        ReportBatch.status.in_(("DISPATCHING", "DISPATCHED", "RUNNING", "WAITING"))
                    )
                )
                or 0
            )
            tenant_active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ReportBatch)
                    .where(
                        ReportBatch.tenant_id == batch.tenant_id,
                        ReportBatch.status.in_(("DISPATCHING", "DISPATCHED", "RUNNING", "WAITING")),
                    )
                )
                or 0
            )
            if global_active >= global_concurrency or tenant_active >= tenant_concurrency:
                raise ResourceNotReady("bulk report concurrency is currently full")
            if (
                batch.status == "DISPATCHING"
                and batch.lease_expires_at is not None
                and batch.lease_expires_at > now
            ):
                raise ResourceNotReady("report batch is already being dispatched")
            preview = await session.scalar(
                select(SelectionPreview).where(
                    SelectionPreview.tenant_id == batch.tenant_id,
                    SelectionPreview.id == report.selection_preview_id,
                )
            )
            if preview is None or preview.payload_ref is None:
                raise ValueError("report selection payload is unavailable")
            if report.policy_snapshot_ref is None or report.data_cutoff is None:
                raise ValueError("report policy or data cutoff is unavailable")
            batch.status = "DISPATCHING"
            batch.attempt_count += 1
            batch.lease_owner = worker_id
            batch.lease_expires_at = now + timedelta(seconds=30)
            if batch.started_at is None:
                batch.started_at = now
            report.status = "RUNNING"
            return ClaimedReportBatch(
                event_id=event_id,
                tenant_id=batch.tenant_id,
                report_run_id=report.id,
                batch_id=batch.id,
                batch_no=batch.batch_no,
                item_offset=batch.item_offset,
                item_count=batch.item_count,
                selection_payload_ref=preview.payload_ref,
                policy_snapshot_ref=report.policy_snapshot_ref,
                data_cutoff=report.data_cutoff,
                lease_owner=worker_id,
            )

    async def record_report_start(
        self,
        claim: ClaimedReportBatch,
        result: RuntimeStartResult,
        *,
        now: datetime,
    ) -> None:
        async with self._factory() as session, session.begin():
            batch = await session.scalar(
                select(ReportBatch)
                .where(
                    ReportBatch.tenant_id == claim.tenant_id,
                    ReportBatch.id == claim.batch_id,
                    ReportBatch.status == "DISPATCHING",
                    ReportBatch.lease_owner == claim.lease_owner,
                )
                .with_for_update()
            )
            if batch is None:
                return
            batch.runtime_execution_id = UUID(result.execution_id)
            batch.status = "DISPATCHED"
            batch.lease_owner = None
            batch.lease_expires_at = None
            session.add(
                ReportInbox(
                    event_id=claim.event_id,
                    tenant_id=claim.tenant_id,
                    event_type="supply-chain.report-batch.requested.v1",
                    processed_at=now,
                )
            )
            session.add(
                ReportActivity(
                    tenant_id=claim.tenant_id,
                    report_run_id=claim.report_run_id,
                    batch_id=claim.batch_id,
                    event_key=f"batch:{claim.batch_id}:dispatched",
                    event_type="report.batch.dispatched",
                    phase="dispatch",
                    state="running",
                    safe_message=f"Batch {claim.batch_no} was dispatched.",
                    payload={"schema_version": "business-agent.activity-event.v1"},
                    critical=True,
                )
            )

    async def record_report_dispatch_error(
        self,
        claim: ClaimedReportBatch,
        *,
        now: datetime,
        retryable: bool,
        safe_error: dict[str, object],
    ) -> None:
        async with self._factory() as session, session.begin():
            batch = await session.scalar(
                select(ReportBatch)
                .where(
                    ReportBatch.tenant_id == claim.tenant_id,
                    ReportBatch.id == claim.batch_id,
                    ReportBatch.status == "DISPATCHING",
                    ReportBatch.lease_owner == claim.lease_owner,
                )
                .with_for_update()
            )
            if batch is None:
                return
            batch.lease_owner = None
            batch.lease_expires_at = None
            batch.safe_error = safe_error
            if retryable and batch.attempt_count < 8:
                batch.status = "QUEUED"
                batch.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(batch.attempt_count, 8))
                )
                state = "retrying"
                message = "Batch dispatch will be retried."
            else:
                batch.status = "FAILED"
                batch.failed_count = batch.item_count
                batch.completed_at = now
                await self._refresh_report(
                    session,
                    report_run_id=batch.report_run_id,
                    tenant_id=batch.tenant_id,
                )
                state = "failed"
                message = str(safe_error.get("safe_message", "Batch dispatch failed."))[:1024]
            session.add(
                ReportActivity(
                    tenant_id=claim.tenant_id,
                    report_run_id=claim.report_run_id,
                    batch_id=claim.batch_id,
                    event_key=f"batch:{claim.batch_id}:dispatch:{batch.attempt_count}",
                    event_type="report.batch.dispatch-failed",
                    phase="dispatch",
                    state=state,
                    safe_message=message,
                    payload={
                        "schema_version": "business-agent.activity-event.v1",
                        "retryable": retryable,
                    },
                    critical=not retryable,
                )
            )

    async def active_report_executions(self) -> list[ActiveReportExecution]:
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(ReportBatch).where(
                        ReportBatch.status.in_(("DISPATCHED", "RUNNING", "WAITING")),
                        ReportBatch.runtime_execution_id.is_not(None),
                    )
                )
            ).all()
        return [
            ActiveReportExecution(
                tenant_id=row.tenant_id,
                report_run_id=row.report_run_id,
                batch_id=row.id,
                execution_id=cast(UUID, row.runtime_execution_id),
                sequence=row.runtime_sequence,
                item_offset=row.item_offset,
                item_count=row.item_count,
            )
            for row in rows
        ]

    async def record_report_snapshot(
        self,
        *,
        target: ActiveReportExecution,
        snapshot: dict[str, Any],
        now: datetime,
    ) -> None:
        runtime_status = snapshot.get("status")
        if runtime_status not in {
            "CREATED",
            "RUNNING",
            "WAITING",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
        }:
            raise ValueError("Runtime report snapshot status is invalid")
        if runtime_status in {"CREATED", "RUNNING", "WAITING"}:
            async with self._factory() as session, session.begin():
                batch = await session.scalar(
                    select(ReportBatch)
                    .where(
                        ReportBatch.tenant_id == target.tenant_id,
                        ReportBatch.id == target.batch_id,
                    )
                    .with_for_update()
                )
                if batch is not None:
                    batch.status = "WAITING" if runtime_status == "WAITING" else "RUNNING"
            return
        complete_count = blocked_count = failed_count = 0
        batch_status = runtime_status
        result_ref: str | None = None
        result_hash: str | None = None
        result_risks: list[str] = []
        if runtime_status == "SUCCEEDED":
            result = validated_batch_output(
                snapshot,
                expected_item_count=target.item_count,
            )
            complete_count = cast(int, result["complete_count"])
            blocked_count = cast(int, result["blocked_count"])
            failed_count = cast(int, result["failed_count"])
            result_ref = cast(str, result["result_artifact_ref"])
            result_hash = cast(str, result["result_artifact_hash"])
            result_risks = cast(list[str], result["risk_flags"])
            artifact = await self._load(target.tenant_id, result_ref, result_hash)
            validated_batch_artifact(
                artifact,
                report_run_id=target.report_run_id,
                batch_id=target.batch_id,
                item_offset=target.item_offset,
                expected_item_count=target.item_count,
                expected_counts=(complete_count, blocked_count, failed_count),
            )
            if failed_count == target.item_count:
                batch_status = "FAILED"
            elif blocked_count == target.item_count:
                batch_status = "BLOCKED"
            elif failed_count or blocked_count:
                batch_status = "PARTIAL"
            else:
                batch_status = "SUCCEEDED"
        async with self._factory() as session, session.begin():
            batch = await session.scalar(
                select(ReportBatch)
                .where(
                    ReportBatch.tenant_id == target.tenant_id,
                    ReportBatch.id == target.batch_id,
                )
                .with_for_update()
            )
            if batch is None or batch.status in {
                "SUCCEEDED",
                "BLOCKED",
                "PARTIAL",
                "FAILED",
                "CANCELLED",
            }:
                return
            batch.status = batch_status
            batch.complete_count = complete_count
            batch.blocked_count = blocked_count
            batch.failed_count = target.item_count if runtime_status == "FAILED" else failed_count
            batch.result_ref = result_ref
            batch.result_hash = result_hash
            batch.completed_at = now
            if runtime_status == "FAILED":
                batch.safe_error = _safe_runtime_error(snapshot.get("error"))
            if result_risks:
                report = await session.scalar(
                    select(ReportRun)
                    .where(
                        ReportRun.tenant_id == batch.tenant_id,
                        ReportRun.id == batch.report_run_id,
                    )
                    .with_for_update()
                )
                if report is not None:
                    existing_risks = {risk for risk in report.risk_flags if isinstance(risk, str)}
                    report.risk_flags = cast(
                        list[object], sorted(existing_risks.union(result_risks))
                    )
            await self._refresh_report(
                session, report_run_id=batch.report_run_id, tenant_id=batch.tenant_id
            )
            session.add(
                ReportActivity(
                    tenant_id=batch.tenant_id,
                    report_run_id=batch.report_run_id,
                    batch_id=batch.id,
                    event_key=f"batch:{batch.id}:terminal",
                    event_type="report.batch.completed",
                    phase="decision",
                    state="completed" if batch_status != "FAILED" else "failed",
                    safe_message=f"Batch {batch.batch_no} reached {batch_status}.",
                    payload={
                        "schema_version": "business-agent.activity-event.v1",
                        "complete_count": complete_count,
                        "blocked_count": blocked_count,
                        "failed_count": failed_count,
                    },
                    critical=True,
                )
            )

    async def _refresh_report(
        self,
        session: AsyncSession,
        *,
        report_run_id: UUID,
        tenant_id: str,
    ) -> None:
        report = await session.scalar(
            select(ReportRun)
            .where(ReportRun.tenant_id == tenant_id, ReportRun.id == report_run_id)
            .with_for_update()
        )
        if report is None:
            return
        batches = (
            await session.scalars(
                select(ReportBatch).where(
                    ReportBatch.tenant_id == tenant_id,
                    ReportBatch.report_run_id == report_run_id,
                )
            )
        ).all()
        terminal = {"SUCCEEDED", "BLOCKED", "PARTIAL", "FAILED", "CANCELLED"}
        if not batches or not all(batch.status in terminal for batch in batches):
            report.status = "RUNNING"
            return
        complete = sum(batch.complete_count for batch in batches)
        blocked = sum(batch.blocked_count for batch in batches)
        failed = sum(batch.failed_count for batch in batches)
        report.complete_count = complete
        report.blocked_count = blocked
        report.failed_count = failed
        if all(batch.status == "CANCELLED" for batch in batches):
            report.status = "CANCELLED"
        elif any(batch.status == "CANCELLED" for batch in batches):
            report.status = "PARTIAL"
        elif failed == report.sku_count:
            report.status = "FAILED"
        elif failed or blocked:
            report.status = "PARTIAL"
        else:
            report.status = "SUCCEEDED"
        report.completed_at = datetime.now(UTC)

    async def create_schedule(
        self, *, tenant_id: str, request: ScheduleCreate, now: datetime
    ) -> dict[str, Any]:
        if request.policy_mode == "PINNED":
            policy = await self._resolve_policy_snapshot(
                tenant_id=tenant_id,
                policy_mode=request.policy_mode,
                policy_version=request.policy_version,
            )
            if policy is None:
                raise ResourceNotReady("pinned policy version does not exist")
        fixed_ref: str | None = None
        fixed_hash: str | None = None
        staged = None
        if request.fixed_skus:
            staged = await self._stage(tenant_id, {"skus": list(request.fixed_skus)})
            fixed_ref = staged.payload_ref
            fixed_hash = staged.payload_hash
        schedule = ReportSchedule(
            tenant_id=tenant_id,
            name=request.name,
            timezone=request.timezone,
            weekday=request.weekday,
            local_time=request.local_time,
            selection_mode=request.selection_mode,
            selector=request.selector.model_dump(mode="json") if request.selector else None,
            fixed_skus_ref=fixed_ref,
            fixed_skus_hash=fixed_hash,
            policy_mode=request.policy_mode,
            policy_version=request.policy_version,
            active=request.active,
            next_fire_at_utc=next_weekly_fire(request, after=now),
        )
        async with self._factory() as session, session.begin():
            session.add(schedule)
        if staged is not None:
            await self._commit_staged(tenant_id, staged)
        return await self._public_schedule(schedule)

    async def create_schedule_run(
        self,
        *,
        tenant_id: str,
        schedule_id: UUID,
        idempotency_key: str,
        now: datetime,
    ) -> UUID | None:
        client_request_id = f"schedule:{schedule_id}:manual:{idempotency_key}"
        async with self._factory() as session, session.begin():
            schedule = await session.scalar(
                select(ReportSchedule).where(
                    ReportSchedule.tenant_id == tenant_id,
                    ReportSchedule.id == schedule_id,
                )
            )
            if schedule is None:
                return None
            existing = await session.scalar(
                select(ReportRun).where(
                    ReportRun.tenant_id == tenant_id,
                    ReportRun.client_request_id == client_request_id,
                )
            )
            if existing is not None:
                return existing.id
            report = ReportRun(
                tenant_id=tenant_id,
                client_request_id=client_request_id,
                selection_preview_id=None,
                schedule_id=schedule.id,
                scheduled_fire_at_utc=now,
                policy_mode=schedule.policy_mode,
                policy_version=schedule.policy_version,
                policy_snapshot_ref=None,
                status="SELECTING",
                sku_count=0,
                batch_count=0,
                next_attempt_at=now,
            )
            session.add(report)
        return report.id

    async def enqueue_due_schedules(self, *, now: datetime, limit: int = 100) -> int:
        created = 0
        async with self._factory() as session, session.begin():
            schedules = (
                await session.scalars(
                    select(ReportSchedule)
                    .where(
                        ReportSchedule.active.is_(True),
                        ReportSchedule.next_fire_at_utc <= now,
                    )
                    .order_by(ReportSchedule.next_fire_at_utc, ReportSchedule.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for schedule in schedules:
                fire_at = schedule.next_fire_at_utc
                client_request_id = f"schedule:{schedule.id}:{fire_at.isoformat()}"
                existing = await session.scalar(
                    select(ReportRun.id).where(
                        ReportRun.tenant_id == schedule.tenant_id,
                        ReportRun.client_request_id == client_request_id,
                    )
                )
                if existing is None:
                    session.add(
                        ReportRun(
                            tenant_id=schedule.tenant_id,
                            client_request_id=client_request_id,
                            selection_preview_id=None,
                            schedule_id=schedule.id,
                            scheduled_fire_at_utc=fire_at,
                            policy_mode=schedule.policy_mode,
                            policy_version=schedule.policy_version,
                            policy_snapshot_ref=None,
                            status="SELECTING",
                            sku_count=0,
                            batch_count=0,
                            next_attempt_at=now,
                        )
                    )
                    created += 1
                template = await self._schedule_template(schedule)
                schedule.next_fire_at_utc = next_weekly_fire(template, after=fire_at)
        return created

    async def claim_scheduled_report(
        self, *, worker_id: str, now: datetime
    ) -> ClaimedScheduledReport | None:
        async with self._factory() as session, session.begin():
            report = await session.scalar(
                select(ReportRun)
                .where(
                    ReportRun.status == "SELECTING",
                    ReportRun.schedule_id.is_not(None),
                    ReportRun.next_attempt_at <= now,
                    or_(
                        ReportRun.lease_expires_at.is_(None),
                        ReportRun.lease_expires_at <= func.current_timestamp(),
                    ),
                )
                .order_by(ReportRun.created_at, ReportRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if report is None or report.schedule_id is None:
                return None
            report.attempt_count += 1
            report.lease_owner = worker_id
            report.lease_expires_at = now + timedelta(minutes=5)
            return ClaimedScheduledReport(
                tenant_id=report.tenant_id,
                report_run_id=report.id,
                schedule_id=report.schedule_id,
                selection_preview_id=report.selection_preview_id,
                lease_owner=worker_id,
            )

    async def prepare_scheduled_selection(
        self,
        claim: ClaimedScheduledReport,
        *,
        now: datetime,
    ) -> UUID:
        if claim.selection_preview_id is not None:
            return claim.selection_preview_id
        async with self._factory() as session:
            schedule = await session.scalar(
                select(ReportSchedule).where(
                    ReportSchedule.tenant_id == claim.tenant_id,
                    ReportSchedule.id == claim.schedule_id,
                )
            )
        if schedule is None:
            raise LookupError("schedule not found")
        client_request_id = f"schedule-selection:{claim.report_run_id}"
        if schedule.selection_mode == "DYNAMIC_SELECTOR":
            preview_id = await self.create_preview(
                tenant_id=claim.tenant_id,
                client_request_id=client_request_id,
                source_kind="SELECTOR",
                request_payload={"selector": dict(schedule.selector or {})},
                selector=dict(schedule.selector or {}),
                now=now,
            )
        else:
            if schedule.fixed_skus_ref is None:
                raise ValueError("fixed schedule has no SKU payload")
            fixed = await self._load(
                claim.tenant_id,
                schedule.fixed_skus_ref,
                schedule.fixed_skus_hash,
            )
            skus = fixed.get("skus")
            if not isinstance(skus, list) or not all(isinstance(sku, str) for sku in skus):
                raise ValueError("fixed schedule SKU payload is invalid")
            parsed = CsvSelection(
                rows=tuple(
                    CsvSelectionRow(row=index + 2, sku=sku) for index, sku in enumerate(skus)
                ),
                errors=(),
                input_row_count=len(skus),
            )
            preview_id = await self.create_csv_preview(
                tenant_id=claim.tenant_id,
                client_request_id=client_request_id,
                parsed=parsed,
                now=now,
            )
        async with self._factory() as session, session.begin():
            report = await session.scalar(
                select(ReportRun)
                .where(
                    ReportRun.tenant_id == claim.tenant_id,
                    ReportRun.id == claim.report_run_id,
                    ReportRun.lease_owner == claim.lease_owner,
                )
                .with_for_update()
            )
            if report is not None:
                report.selection_preview_id = preview_id
                report.lease_owner = None
                report.lease_expires_at = None
                report.next_attempt_at = now
        return preview_id

    async def materialize_scheduled_report(
        self,
        claim: ClaimedScheduledReport,
        *,
        now: datetime,
        trace_id: str,
    ) -> bool:
        async with self._factory() as session:
            report_configuration = await session.scalar(
                select(ReportRun).where(
                    ReportRun.tenant_id == claim.tenant_id,
                    ReportRun.id == claim.report_run_id,
                )
            )
        if report_configuration is None:
            return False
        policy = await self._resolve_policy_snapshot(
            tenant_id=claim.tenant_id,
            policy_mode=report_configuration.policy_mode,
            policy_version=report_configuration.policy_version,
        )
        async with self._factory() as session, session.begin():
            report = await session.scalar(
                select(ReportRun)
                .where(
                    ReportRun.tenant_id == claim.tenant_id,
                    ReportRun.id == claim.report_run_id,
                )
                .with_for_update()
            )
            if report is None or report.selection_preview_id is None:
                return False
            preview = await session.scalar(
                select(SelectionPreview)
                .where(
                    SelectionPreview.tenant_id == claim.tenant_id,
                    SelectionPreview.id == report.selection_preview_id,
                )
                .with_for_update()
            )
            if preview is None or preview.status in {"PLANNING", "DISCOVERING", "PROCESSING"}:
                report.lease_owner = None
                report.lease_expires_at = None
                report.next_attempt_at = now + timedelta(seconds=1)
                return False
            if (
                preview.status != "READY"
                or preview.snapshot_time is None
                or not 1 <= preview.matched_count <= 10_000
            ):
                report.status = "FAILED"
                report.failed_count = preview.matched_count
                report.completed_at = now
                report.lease_owner = None
                report.lease_expires_at = None
                return True
            if policy is None:
                report.status = "FAILED"
                report.completed_at = now
                report.lease_owner = None
                report.lease_expires_at = None
                return True
            report.policy_version = policy.version
            report.policy_snapshot_ref = policy.document_ref
            report.data_cutoff = preview.snapshot_time
            report.risk_flags = list(dict.fromkeys([*report.risk_flags, *policy.risk_flags]))
            report.sku_count = preview.matched_count
            report.batch_count = (preview.matched_count + 199) // 200
            report.status = "ACCEPTED"
            report.lease_owner = None
            report.lease_expires_at = None
            preview.confirmed = True
            report_batches: list[ReportBatch] = []
            outbox: list[ReportOutbox] = []
            for index in range(report.batch_count):
                batch_id = uuid4()
                item_count = min(200, report.sku_count - index * 200)
                report_batches.append(
                    ReportBatch(
                        id=batch_id,
                        tenant_id=claim.tenant_id,
                        report_run_id=report.id,
                        batch_no=index + 1,
                        item_offset=index * 200,
                        item_count=item_count,
                        status="QUEUED",
                        next_attempt_at=now,
                    )
                )
                event_id = uuid4()
                outbox.append(
                    ReportOutbox(
                        event_id=event_id,
                        tenant_id=claim.tenant_id,
                        report_run_id=report.id,
                        batch_id=batch_id,
                        event_type="supply-chain.report-batch.requested.v1",
                        payload={
                            "event_id": str(event_id),
                            "schema_version": 1,
                            "report_run_id": str(report.id),
                            "batch_id": str(batch_id),
                            "selection_snapshot_id": str(preview.id),
                            "batch_no": index + 1,
                            "item_count": item_count,
                            "policy_snapshot_ref": policy.document_ref,
                            "trace_id": trace_id,
                            "occurred_at": now.isoformat(),
                        },
                        status="PENDING",
                        next_attempt_at=now,
                    )
                )
            session.add_all(report_batches)
            await session.flush()
            session.add_all(outbox)
            session.add(
                ReportActivity(
                    tenant_id=claim.tenant_id,
                    report_run_id=report.id,
                    event_key="report:accepted",
                    event_type="report.accepted",
                    phase="selection",
                    state="accepted",
                    safe_message=(
                        f"Scheduled report selected {report.sku_count} SKU(s) "
                        f"in {report.batch_count} batch(es)."
                    ),
                    progress_current=0,
                    progress_total=report.batch_count,
                    payload={"schema_version": "business-agent.activity-event.v1"},
                    critical=True,
                )
            )
        return True

    async def retry_scheduled_report(
        self,
        claim: ClaimedScheduledReport,
        *,
        now: datetime,
        safe_message: str,
    ) -> None:
        async with self._factory() as session, session.begin():
            report = await session.scalar(
                select(ReportRun)
                .where(
                    ReportRun.tenant_id == claim.tenant_id,
                    ReportRun.id == claim.report_run_id,
                    ReportRun.status == "SELECTING",
                    ReportRun.lease_owner == claim.lease_owner,
                )
                .with_for_update()
            )
            if report is None:
                return
            report.lease_owner = None
            report.lease_expires_at = None
            if report.attempt_count >= 5:
                report.status = "FAILED"
                report.completed_at = now
                state = "failed"
            else:
                report.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(report.attempt_count, 8))
                )
                state = "retrying"
            session.add(
                ReportActivity(
                    tenant_id=claim.tenant_id,
                    report_run_id=claim.report_run_id,
                    event_key=f"schedule:{claim.report_run_id}:attempt:{report.attempt_count}",
                    event_type="schedule.selection-failed",
                    phase="selection",
                    state=state,
                    safe_message=safe_message[:1024],
                    payload={"schema_version": "business-agent.activity-event.v1"},
                    critical=state == "failed",
                )
            )

    async def list_schedules(self, *, tenant_id: str) -> list[dict[str, Any]]:
        async with self._factory() as session:
            schedules = (
                await session.scalars(
                    select(ReportSchedule)
                    .where(ReportSchedule.tenant_id == tenant_id)
                    .order_by(ReportSchedule.created_at, ReportSchedule.id)
                )
            ).all()
        return [await self._public_schedule(schedule) for schedule in schedules]

    async def update_schedule(
        self,
        *,
        tenant_id: str,
        schedule_id: UUID,
        patch: SchedulePatch,
        now: datetime,
    ) -> dict[str, Any] | None:
        async with self._factory() as session, session.begin():
            schedule = await session.scalar(
                select(ReportSchedule)
                .where(
                    ReportSchedule.tenant_id == tenant_id,
                    ReportSchedule.id == schedule_id,
                )
                .with_for_update()
            )
            if schedule is None:
                return None
            values = patch.model_dump(exclude_unset=True)
            for name, value in values.items():
                setattr(schedule, name, value)
            template = ScheduleCreate(
                name=schedule.name,
                timezone=schedule.timezone,
                weekday=schedule.weekday,
                local_time=schedule.local_time,
                selection_mode=cast(Any, schedule.selection_mode),
                selector=(
                    InventorySelector.model_validate(schedule.selector)
                    if schedule.selector is not None
                    else None
                ),
                fixed_skus=("PAYLOAD_REF",) if schedule.fixed_skus_ref else (),
                policy_mode=cast(Any, schedule.policy_mode),
                policy_version=schedule.policy_version,
                active=schedule.active,
            )
            schedule.next_fire_at_utc = next_weekly_fire(template, after=now)
        return await self._public_schedule(schedule)

    async def _resolve_policy_snapshot(
        self,
        *,
        tenant_id: str,
        policy_mode: str,
        policy_version: int | None,
    ) -> ResolvedPolicySnapshot | None:
        query = select(PolicyVersion).where(PolicyVersion.tenant_id == tenant_id)
        if policy_mode == "PINNED":
            query = query.where(PolicyVersion.version == policy_version)
        else:
            query = query.where(PolicyVersion.active.is_(True))
        async with self._factory() as session:
            policy = cast(PolicyVersion | None, await session.scalar(query))
        if policy is not None:
            return ResolvedPolicySnapshot(
                version=policy.version,
                document_ref=policy.document_ref,
            )
        if policy_mode == "PINNED":
            return None
        document = default_policy_document()
        validate_policy(document)
        staged = await self._stage(tenant_id, document)
        await self._commit_staged(tenant_id, staged)
        return ResolvedPolicySnapshot(
            version=None,
            document_ref=cast(str, staged.payload_ref),
            risk_flags=("POLICY_DEFAULTED", "CAPITAL_COST_DEFAULTED_1_PERCENT"),
        )

    async def _public_policy(self, policy: PolicyVersion, document: object) -> dict[str, Any]:
        return {
            "version": policy.version,
            "digest": policy.digest,
            "active": policy.active,
            "published_at": policy.published_at.isoformat(),
            "warnings": list(policy.warnings),
            "document": document,
        }

    async def _public_schedule(self, schedule: ReportSchedule) -> dict[str, Any]:
        fixed_skus: list[object] = []
        if schedule.fixed_skus_ref:
            payload = await self._load(
                schedule.tenant_id,
                schedule.fixed_skus_ref,
                schedule.fixed_skus_hash,
            )
            if isinstance(payload, dict) and isinstance(payload.get("skus"), list):
                fixed_skus = payload["skus"]
        return {
            "schedule_id": str(schedule.id),
            "name": schedule.name,
            "timezone": schedule.timezone,
            "weekday": schedule.weekday,
            "local_time": schedule.local_time.strftime("%H:%M"),
            "selection_mode": schedule.selection_mode,
            "selector": schedule.selector,
            "fixed_skus": fixed_skus,
            "policy_mode": schedule.policy_mode,
            "policy_version": schedule.policy_version,
            "active": schedule.active,
            "next_fire_at_utc": schedule.next_fire_at_utc.isoformat(),
        }

    async def _schedule_template(self, schedule: ReportSchedule) -> ScheduleCreate:
        return ScheduleCreate(
            name=schedule.name,
            timezone=schedule.timezone,
            weekday=schedule.weekday,
            local_time=schedule.local_time,
            selection_mode=cast(Any, schedule.selection_mode),
            selector=(
                InventorySelector.model_validate(schedule.selector)
                if schedule.selector is not None
                else None
            ),
            fixed_skus=("PAYLOAD_REF",) if schedule.fixed_skus_ref else (),
            policy_mode=cast(Any, schedule.policy_mode),
            policy_version=schedule.policy_version,
            active=schedule.active,
        )

    async def _stage(self, tenant_id: str, payload: Mapping[str, object]) -> Any:
        staged = await self._payload_store.put_exact_restricted(
            tenant_id=tenant_id,
            payload=cast(JsonValue, dict(payload)),
            required_permission=_PAYLOAD_PERMISSION,
        )
        if staged.payload_ref is None:
            raise RuntimeError("Level 2 payload must use controlled external storage")
        return staged

    async def _commit_staged(self, tenant_id: str, staged: Any) -> None:
        await self._payload_store.ensure_committed(
            tenant_id=tenant_id,
            payload_ref=staged.payload_ref,
            payload_hash=staged.payload_hash,
            size_bytes=staged.size_bytes,
            content_type=staged.content_type,
            classification=staged.classification,
            required_permission=staged.required_permission,
        )

    async def _load(
        self,
        tenant_id: str,
        payload_ref: str | None,
        expected_hash: str | None,
    ) -> dict[str, Any]:
        if payload_ref is None:
            return {}
        try:
            data = await self._payload_store.get_authorized(
                tenant_id=tenant_id,
                payload_ref=payload_ref,
                permission_scope=_PAYLOAD_PERMISSION,
            )
        except PayloadAuthorizationError:
            staged = await self._payload_store.inspect_for_finalization(
                tenant_id=tenant_id,
                payload_ref=payload_ref,
                permission_scope=_PAYLOAD_PERMISSION,
            )
            if expected_hash is not None and staged.payload_hash != expected_hash:
                raise ValueError("payload identity mismatch")
            await self._commit_staged(tenant_id, staged)
            data = await self._payload_store.get_authorized(
                tenant_id=tenant_id,
                payload_ref=payload_ref,
                permission_scope=_PAYLOAD_PERMISSION,
            )
        if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError("payload hash mismatch")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("payload contract is invalid")
        return value


def next_weekly_fire(schedule: ScheduleCreate, *, after: datetime) -> datetime:
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("after must carry timezone")
    try:
        zone = ZoneInfo(schedule.timezone)
    except ZoneInfoNotFoundError:
        raise ValueError("timezone is not recognized") from None
    local_after = after.astimezone(zone)
    days = (schedule.weekday - local_after.isoweekday()) % 7
    candidate_date = local_after.date() + timedelta(days=days)
    candidate = datetime.combine(candidate_date, schedule.local_time, tzinfo=zone)
    if candidate <= local_after:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def _safe_runtime_error(value: object) -> dict[str, object]:
    error = value if isinstance(value, Mapping) else {}
    error_code = error.get("error_code")
    category = error.get("category")
    phase = error.get("phase")
    retryable = error.get("retryable")
    safe_message = error.get("safe_message")
    request_id = error.get("request_id")
    return {
        "error_code": (
            error_code[:128]
            if isinstance(error_code, str) and error_code
            else "RUNTIME_EXECUTION_FAILED"
        ),
        "category": category[:128] if isinstance(category, str) and category else "permanent",
        "phase": phase[:128] if isinstance(phase, str) and phase else "invocation",
        "retryable": retryable if isinstance(retryable, bool) else False,
        "safe_message": (
            safe_message[:1000]
            if isinstance(safe_message, str) and safe_message
            else "The Runtime execution failed."
        ),
        "request_id": request_id[:128] if isinstance(request_id, str) and request_id else None,
    }
