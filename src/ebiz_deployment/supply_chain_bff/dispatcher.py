"""Persistent dispatcher, Runtime fan-in, polling fallback, and restart recovery."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

import jwt

from .activity import project_runtime_event
from .config import BffSettings
from .repository import ActiveExecution, BatchRepository
from .runtime_client import RuntimeClient, RuntimeRequestError


class BatchCoordinator:
    def __init__(
        self,
        *,
        repository: BatchRepository,
        runtime: RuntimeClient,
        settings: BffSettings,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._settings = settings
        self._worker_id = f"supply-chain-bff:{uuid4().hex}"
        self._stream_supported = settings.stream_enabled
        self._stream_tasks: dict[tuple[str, object], asyncio.Task[None]] = {}
        self._stream_members: dict[tuple[str, object], frozenset[UUID]] = {}
        self._last_retention_run = 0.0

    async def dispatch_once(self, *, limit: int = 32) -> int:
        dispatched = 0
        for _ in range(limit):
            claim = await self._repository.claim_item(
                worker_id=self._worker_id,
                now=datetime.now(UTC),
            )
            if claim is None:
                break
            try:
                payload = await self._repository.load_payload(claim)
                sku = payload.skus[claim.item_index]
                result = await self._runtime.start(
                    authorization=self.authorization_for_tenant(claim.tenant_id),
                    respond_async=self._settings.async_start_enabled,
                    payload={
                        "agent": {"id": "inventory-supply-chain", "version": 4},
                        "workflow": {
                            "code": "inventory-supply-chain-daily",
                            "version": 4,
                        },
                        "inputs": {
                            "run_id": f"{claim.batch_id}:{claim.item_index}",
                            "market_scope": "NA_COMPANY",
                            "sku": sku,
                            "snapshot_time": claim.snapshot_time.isoformat(),
                            "skill_input_ref": self._settings.skill_input_ref,
                            "fulfillment_mode": "FBM",
                        },
                        "idempotency_key": f"supply-chain:{claim.batch_id}:{claim.item_index}",
                    },
                )
                await self._repository.record_start(claim, result)
                if result.snapshot.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    await self._repository.record_snapshot(
                        tenant_id=claim.tenant_id,
                        execution_id=UUID(result.execution_id),
                        snapshot=result.snapshot,
                    )
                dispatched += 1
            except Exception as error:
                await self._repository.record_dispatch_error(claim, error)
        return dispatched

    async def reconcile_polling_once(self, targets: list[ActiveExecution] | None = None) -> int:
        active = (
            targets if targets is not None else await self._repository.active_execution_targets()
        )
        reconciled = 0
        for target in active:
            try:
                snapshot = await self._runtime.get_execution(
                    authorization=self.authorization_for_tenant(target.tenant_id),
                    execution_id=str(target.execution_id),
                )
                await self._repository.record_snapshot(
                    tenant_id=target.tenant_id,
                    execution_id=target.execution_id,
                    snapshot=snapshot,
                )
                reconciled += 1
            except RuntimeRequestError:
                continue
        return reconciled

    async def run_forever(self, *, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                await self.dispatch_once()
                targets = await self._repository.active_execution_targets()
                self._prune_stream_tasks()
                covered: set[tuple[str, object]] = set()
                if self._stream_supported:
                    grouped: dict[tuple[str, object], list[ActiveExecution]] = defaultdict(list)
                    for target in targets:
                        grouped[(target.tenant_id, target.batch_id)].append(target)
                    for key, batch_targets in grouped.items():
                        covered.add(key)
                        members = frozenset(target.execution_id for target in batch_targets)
                        if key in self._stream_tasks and self._stream_members.get(key) != members:
                            self._stream_tasks[key].cancel()
                            del self._stream_tasks[key]
                        if key not in self._stream_tasks:
                            self._stream_tasks[key] = asyncio.create_task(
                                self._fan_in(batch_targets),
                                name=f"supply-chain-fan-in:{batch_targets[0].batch_id}",
                            )
                            self._stream_members[key] = members
                polling_targets = [
                    target
                    for target in targets
                    if not self._stream_supported
                    or (target.tenant_id, target.batch_id) not in covered
                ]
                if polling_targets:
                    await self.reconcile_polling_once(polling_targets)
                loop_time = asyncio.get_running_loop().time()
                if loop_time - self._last_retention_run >= 3600:
                    await self._repository.prune_retention(
                        now=datetime.now(UTC),
                        completed_activity_days=self._settings.completed_activity_retention_days,
                        batch_mapping_days=self._settings.batch_mapping_retention_days,
                    )
                    self._last_retention_run = loop_time
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.db_tail_seconds)
                except TimeoutError:
                    continue
        finally:
            for task in self._stream_tasks.values():
                task.cancel()
            for task in self._stream_tasks.values():
                with suppress(asyncio.CancelledError):
                    await task
            self._stream_tasks.clear()
            self._stream_members.clear()

    async def _fan_in(self, targets: list[ActiveExecution]) -> None:
        first = targets[0]
        try:
            async for event in self._runtime.stream(
                authorization=self.authorization_for_tenant(first.tenant_id),
                sequences={str(target.execution_id): target.sequence for target in targets},
            ):
                await self._repository.record_runtime_event(
                    tenant_id=first.tenant_id,
                    batch_id=first.batch_id,
                    projection=project_runtime_event(event),
                )
            await self.reconcile_polling_once(targets)
        except RuntimeRequestError as error:
            if error.status_code in {404, 405, 415, 422}:
                self._stream_supported = False
            await self.reconcile_polling_once(targets)

    def authorization_for_tenant(
        self,
        tenant_id: str,
        *,
        include_model_feedback: bool = False,
    ) -> str:
        """Mint a short-lived service token; browser credentials are never persisted."""

        issued_at = int(time.time())
        scopes = [
            "inventory.read",
            "sales_profit.read",
            "supply_chain.preview",
            "workflow:read",
            "workflow:start",
        ]
        if include_model_feedback:
            scopes.append("model:feedback")
        token = jwt.encode(
            {
                "aud": "agent-runtime",
                "sub": "7725f5d2-609f-54f0-99f9-83bfecf877b1",
                "tenant_id": tenant_id,
                "actor_type": "service",
                "scopes": scopes,
                "roles": ["supply-chain-bff"],
                "credential_ref": self._settings.runtime_credential_ref,
                "iat": issued_at,
                "exp": issued_at + 300,
            },
            self._settings.jwt_secret,
            algorithm="HS256",
        )
        return f"Bearer {token}"

    def _prune_stream_tasks(self) -> None:
        for key, task in list(self._stream_tasks.items()):
            if task.done():
                with suppress(Exception):
                    task.result()
                del self._stream_tasks[key]
                self._stream_members.pop(key, None)
