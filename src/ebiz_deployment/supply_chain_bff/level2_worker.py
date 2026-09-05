"""Durable Level 2 selection, outbox, RabbitMQ, and Runtime coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from .activity import project_runtime_event
from .config import BffSettings
from .level2_contracts import InventorySelector
from .level2_repository import (
    ActiveReportExecution,
    ClaimedPreview,
    ClaimedReportBatch,
    Level2Repository,
    ResourceNotReady,
)
from .report_mq import DeferredDelivery, ReportBatchMessage, ReportMessageBus
from .runtime_client import RuntimeClient, RuntimeRequestError, RuntimeStartResult

_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
AuthorizationFactory = Callable[[str], str]


class Level2Worker:
    def __init__(
        self,
        *,
        repository: Level2Repository,
        runtime: RuntimeClient,
        settings: BffSettings,
        authorization_for_tenant: AuthorizationFactory,
        bus: ReportMessageBus | None,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._settings = settings
        self._authorization_for_tenant = authorization_for_tenant
        self._bus = bus
        self._worker_id = f"supply-chain-level2:{uuid4().hex}"
        self._stream_supported = settings.stream_enabled
        self._stream_tasks: dict[UUID, asyncio.Task[None]] = {}

    async def run_forever(self, *, stop: asyncio.Event) -> None:
        tasks: list[asyncio.Task[None]] = []
        if self._bus is not None:
            await self._bus.connect()
            tasks.append(
                asyncio.create_task(
                    self._bus.consume(self._handle_delivery, stop=stop),
                    name="supply-chain-level2-mq-consumer",
                )
            )
        try:
            while not stop.is_set():
                await self._repository.enqueue_due_schedules(now=datetime.now(UTC))
                await self.process_scheduled_report_once()
                await self.process_selection_once()
                if self._bus is not None:
                    await self.publish_outbox_once()
                targets = await self._repository.active_report_executions()
                self._prune_stream_tasks()
                if self._stream_supported:
                    for target in targets:
                        if target.execution_id not in self._stream_tasks:
                            self._stream_tasks[target.execution_id] = asyncio.create_task(
                                self._stream_execution(target),
                                name=f"supply-chain-level2-stream:{target.execution_id}",
                            )
                await self.reconcile_once(targets)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.db_tail_seconds)
                except TimeoutError:
                    continue
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            for task in self._stream_tasks.values():
                task.cancel()
            for task in self._stream_tasks.values():
                with suppress(asyncio.CancelledError):
                    await task
            self._stream_tasks.clear()
            if self._bus is not None:
                await self._bus.close()

    async def process_selection_once(self) -> bool:
        now = datetime.now(UTC)
        claim = await self._repository.claim_preview(worker_id=self._worker_id, now=now)
        if claim is None:
            return False
        try:
            snapshot = await self._run_selection(claim)
            result = _selection_result(snapshot)
            await self._repository.complete_preview(
                tenant_id=claim.tenant_id,
                preview_id=claim.preview_id,
                lease_owner=claim.lease_owner,
                selector=result["selector"],
                source_snapshot_id=result["source_snapshot_id"],
                snapshot_time=_timestamp(result["snapshot_time"]),
                rows=result["rows"],
                warnings=result.get("warnings", []),
                ambiguities=result.get("ambiguities", []),
            )
        except RuntimeRequestError as error:
            await self._repository.retry_preview(
                claim,
                now=now,
                safe_warning=error.safe_message,
                terminal=not error.retryable,
            )
        except (TypeError, ValueError) as error:
            await self._repository.retry_preview(
                claim,
                now=now,
                safe_warning=str(error),
                terminal=True,
            )
        return True

    async def process_scheduled_report_once(self) -> bool:
        now = datetime.now(UTC)
        claim = await self._repository.claim_scheduled_report(
            worker_id=self._worker_id,
            now=now,
        )
        if claim is None:
            return False
        try:
            if claim.selection_preview_id is None:
                await self._repository.prepare_scheduled_selection(claim, now=now)
                return True
            await self._repository.materialize_scheduled_report(
                claim,
                now=now,
                trace_id=f"schedule:{claim.report_run_id}",
            )
        except (LookupError, TypeError, ValueError) as error:
            await self._repository.retry_scheduled_report(
                claim,
                now=now,
                safe_message=str(error),
            )
            return False
        return True

    async def publish_outbox_once(self) -> bool:
        if self._bus is None:
            return False
        now = datetime.now(UTC)
        claim = await self._repository.claim_outbox(worker_id=self._worker_id, now=now)
        if claim is None:
            return False
        try:
            await self._bus.publish(claim.payload)
        except Exception:
            await self._repository.mark_outbox_retry(claim, now=now)
            return False
        await self._repository.mark_outbox_published(claim, now=now)
        return True

    async def reconcile_once(self, targets: list[ActiveReportExecution] | None = None) -> int:
        reconciled = 0
        active = (
            targets if targets is not None else await self._repository.active_report_executions()
        )
        for target in active:
            try:
                snapshot = await self._runtime.get_execution(
                    authorization=self._authorization_for_tenant(target.tenant_id),
                    execution_id=str(target.execution_id),
                )
                await self._repository.record_report_snapshot(
                    target=target,
                    snapshot=snapshot,
                    now=datetime.now(UTC),
                )
                reconciled += 1
            except RuntimeRequestError:
                continue
        return reconciled

    async def _stream_execution(self, target: ActiveReportExecution) -> None:
        try:
            async for event in self._runtime.stream(
                authorization=self._authorization_for_tenant(target.tenant_id),
                sequences={str(target.execution_id): target.sequence},
            ):
                await self._repository.record_report_runtime_event(
                    target=target,
                    projection=project_runtime_event(event),
                )
        except RuntimeRequestError as error:
            if error.status_code in {404, 405, 415, 422}:
                self._stream_supported = False

    def _prune_stream_tasks(self) -> None:
        for execution_id, task in list(self._stream_tasks.items()):
            if not task.done():
                continue
            with suppress(Exception):
                task.result()
            del self._stream_tasks[execution_id]

    async def cancel_report(self, *, tenant_id: str, report_run_id: UUID) -> bool:
        targets = [
            target
            for target in await self._repository.active_report_executions()
            if target.tenant_id == tenant_id and target.report_run_id == report_run_id
        ]
        found = await self._repository.cancel_report(
            tenant_id=tenant_id,
            report_run_id=report_run_id,
        )
        if not found:
            return False
        for target in targets:
            try:
                await self._runtime.cancel(
                    authorization=self._authorization_for_tenant(tenant_id),
                    execution_id=str(target.execution_id),
                )
            except RuntimeRequestError:
                continue
        return True

    async def _run_selection(self, claim: ClaimedPreview) -> dict[str, Any]:
        request = await self._repository.load_preview_request(claim)
        if not request:
            raise ValueError("selection request payload is empty")
        planner_ambiguities: list[str] = []
        if claim.source_kind == "NATURAL_LANGUAGE":
            natural_language = request.get("natural_language")
            if not isinstance(natural_language, str) or not natural_language:
                raise ValueError("natural-language selection request is invalid")
            planner = await self._runtime.start(
                authorization=self._authorization_for_tenant(claim.tenant_id),
                respond_async=self._settings.async_start_enabled,
                payload={
                    "agent": {"id": "inventory-supply-chain", "version": 6},
                    "workflow": {"code": "inventory-selection-request-planner", "version": 6},
                    "inputs": {
                        "natural_language": natural_language,
                        "request_time": claim.requested_at.isoformat(),
                    },
                    "idempotency_key": f"supply-chain-selection-plan:{claim.preview_id}",
                },
            )
            selector, planner_ambiguities = _planner_result(
                await self._terminal_snapshot(claim.tenant_id, planner)
            )
        else:
            selector = InventorySelector.model_validate(claim.selector).model_dump(mode="json")
        pages: list[dict[str, Any]] = []
        source_snapshot_id: str | None = None
        cursor: str | None = None
        for page_no in range(51):
            execution_request = _selection_runtime_payload(
                preview_id=claim.preview_id,
                selector=selector,
                page_no=page_no,
                source_snapshot_id=source_snapshot_id,
                cursor=cursor,
            )
            started = await self._runtime.start(
                authorization=self._authorization_for_tenant(claim.tenant_id),
                respond_async=self._settings.async_start_enabled,
                payload=execution_request,
            )
            snapshot = await self._terminal_snapshot(claim.tenant_id, started)
            page = _selection_result(snapshot, selector=selector)
            pages.append(page)
            source_snapshot_id = cast(str, page["source_snapshot_id"])
            cursor = cast(str | None, page["next_cursor"])
            if cursor is None or sum(len(value["rows"]) for value in pages) >= 10_001:
                break
        result = _merge_selection_pages(pages)
        result["ambiguities"] = list(
            dict.fromkeys([*planner_ambiguities, *result.get("ambiguities", [])])
        )
        return {"status": "SUCCEEDED", "outputs": {"result": result}}

    async def _handle_delivery(self, message: ReportBatchMessage) -> None:
        now = datetime.now(UTC)
        try:
            claim = await self._repository.claim_report_delivery(
                message=message,
                worker_id=self._worker_id,
                now=now,
                tenant_concurrency=self._settings.tenant_bulk_concurrency,
                global_concurrency=self._settings.global_bulk_concurrency,
            )
        except ResourceNotReady:
            raise DeferredDelivery("report capacity is full") from None
        if claim is None:
            return
        try:
            result = await self._runtime.start(
                authorization=self._authorization_for_tenant(claim.tenant_id),
                respond_async=self._settings.async_start_enabled,
                payload=_batch_runtime_payload(claim),
            )
            await self._repository.record_report_start(claim, result, now=now)
            if result.snapshot.get("status") in _TERMINAL:
                await self._repository.record_report_snapshot(
                    target=ActiveReportExecution(
                        tenant_id=claim.tenant_id,
                        report_run_id=claim.report_run_id,
                        batch_id=claim.batch_id,
                        execution_id=UUID(result.execution_id),
                        sequence=0,
                        item_offset=claim.item_offset,
                        item_count=claim.item_count,
                    ),
                    snapshot=result.snapshot,
                    now=datetime.now(UTC),
                )
        except RuntimeRequestError as error:
            await self._repository.record_report_dispatch_error(
                claim,
                now=now,
                retryable=error.retryable,
                safe_error={
                    "phase": error.phase,
                    "category": error.category,
                    "retryable": error.retryable,
                    "safe_message": error.safe_message,
                    "request_id": error.request_id,
                },
            )
            if error.retryable:
                raise DeferredDelivery(error.safe_message) from None
        except (TypeError, ValueError) as error:
            await self._repository.record_report_dispatch_error(
                claim,
                now=now,
                retryable=False,
                safe_error={
                    "phase": "validation",
                    "category": "contract",
                    "retryable": False,
                    "safe_message": str(error)[:1024],
                    "request_id": None,
                },
            )

    async def _terminal_snapshot(
        self,
        tenant_id: str,
        started: RuntimeStartResult,
    ) -> dict[str, Any]:
        snapshot = started.snapshot
        if snapshot.get("status") in _TERMINAL:
            return snapshot
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.etl_wait_seconds
        while loop.time() < deadline:
            await asyncio.sleep(min(self._settings.db_tail_seconds, 1.0))
            snapshot = await self._runtime.get_execution(
                authorization=self._authorization_for_tenant(tenant_id),
                execution_id=started.execution_id,
            )
            if snapshot.get("status") in _TERMINAL:
                return snapshot
        raise RuntimeRequestError(
            status_code=504,
            error_code="SELECTION_RUNTIME_TIMEOUT",
            phase="invocation",
            category="transient",
            retryable=True,
            safe_message="Selection preview is still waiting for Runtime completion.",
            request_id=None,
        )


def _batch_runtime_payload(
    claim: ClaimedReportBatch,
) -> dict[str, object]:
    cutoff_date = claim.data_cutoff.date()
    week_to = cutoff_date - timedelta(days=cutoff_date.weekday() + 7)
    week_from = week_to - timedelta(weeks=259)
    return {
        "agent": {"id": "inventory-supply-chain", "version": 6},
        "workflow": {"code": "inventory-supply-chain-batch-weekly", "version": 6},
        "inputs": {
            "report_run_id": str(claim.report_run_id),
            "batch_id": str(claim.batch_id),
            "selection_snapshot_ref": claim.selection_payload_ref,
            "item_offset": claim.item_offset,
            "item_count": claim.item_count,
            "policy_snapshot_ref": claim.policy_snapshot_ref,
            "data_cutoff": claim.data_cutoff.isoformat().replace("+00:00", "Z"),
            "week_from": week_from.isoformat(),
            "week_to": week_to.isoformat(),
            "summary_enabled": True,
        },
        "idempotency_key": f"supply-chain-report:{claim.batch_id}",
    }


def _selection_runtime_payload(
    *,
    preview_id: UUID,
    selector: dict[str, object],
    page_no: int,
    source_snapshot_id: str | None,
    cursor: str | None,
) -> dict[str, object]:
    quantity_metric = selector.get("quantity_metric")
    operator = selector.get("operator")
    threshold = selector.get("threshold")
    if quantity_metric != "AVAILABLE_QUANTITY" or operator != "GT":
        raise ValueError("selection selector is unsupported")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
        raise ValueError("selection threshold is invalid")
    inputs: dict[str, object] = {
        "preview_id": str(preview_id),
        "quantity_metric": quantity_metric,
        "operator": operator,
        "threshold": threshold,
        "sort": "SKU_ASC",
        "page_size": 200,
    }
    workflow_code = "inventory-selection-discovery"
    if source_snapshot_id is not None or cursor is not None:
        if not source_snapshot_id or not cursor:
            raise ValueError("selection continuation requires snapshot and cursor")
        workflow_code = "inventory-selection-discovery-continuation"
        inputs["source_snapshot_id"] = source_snapshot_id
        inputs["cursor"] = cursor
    return {
        "agent": {"id": "inventory-supply-chain", "version": 6},
        "workflow": {"code": workflow_code, "version": 6},
        "inputs": inputs,
        "idempotency_key": f"supply-chain-selection-discovery:{preview_id}:page:{page_no}",
    }


def _selection_result(
    snapshot: dict[str, Any],
    *,
    selector: dict[str, object] | None = None,
) -> dict[str, Any]:
    if snapshot.get("status") != "SUCCEEDED":
        raise ValueError("selection workflow did not succeed")
    outputs = snapshot.get("outputs")
    result = outputs.get("result") if isinstance(outputs, dict) else None
    result = _agent_result_payload(result)
    if not isinstance(result, dict):
        raise ValueError("selection workflow omitted result")
    if selector is not None and set(result) == {
        "schema_version",
        "source_snapshot_id",
        "snapshot_time",
        "items",
        "next_cursor",
    }:
        result = {
            "selector": selector,
            "source_snapshot_id": result["source_snapshot_id"],
            "snapshot_time": result["snapshot_time"],
            "rows": result["items"],
            "warnings": [],
            "ambiguities": [],
            "next_cursor": result["next_cursor"],
        }
    normalized_selector = result.get("selector")
    snapshot_id = result.get("source_snapshot_id")
    snapshot_time = result.get("snapshot_time")
    rows = result.get("rows")
    if not isinstance(normalized_selector, dict):
        raise ValueError("selection workflow omitted selector")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("selection workflow omitted source_snapshot_id")
    if not isinstance(snapshot_time, str):
        raise ValueError("selection workflow omitted snapshot_time")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("selection workflow rows are invalid")
    warnings = result.get("warnings", [])
    ambiguities = result.get("ambiguities", [])
    next_cursor = result.get("next_cursor")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("selection workflow warnings are invalid")
    if not isinstance(ambiguities, list) or not all(isinstance(item, str) for item in ambiguities):
        raise ValueError("selection workflow ambiguities are invalid")
    if next_cursor is not None and (
        not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 2048
    ):
        raise ValueError("selection workflow cursor is invalid")
    return {
        "selector": normalized_selector,
        "source_snapshot_id": snapshot_id,
        "snapshot_time": snapshot_time,
        "rows": rows,
        "warnings": warnings,
        "ambiguities": ambiguities,
        "next_cursor": next_cursor,
    }


def _merge_selection_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    if not pages:
        raise ValueError("selection discovery returned no pages")
    first = pages[0]
    selector = first["selector"]
    snapshot_id = first["source_snapshot_id"]
    snapshot_time = first["snapshot_time"]
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    ambiguities: list[str] = []
    seen_cursors: set[str] = set()
    previous_sku: str | None = None
    for page_no, page in enumerate(pages):
        if (
            page["selector"] != selector
            or page["source_snapshot_id"] != snapshot_id
            or page["snapshot_time"] != snapshot_time
        ):
            raise ValueError("selection discovery snapshot changed between pages")
        page_rows = page["rows"]
        if len(page_rows) > 200:
            raise ValueError("selection discovery page exceeds 200 rows")
        for row in page_rows:
            sku = row.get("sku")
            if not isinstance(sku, str) or not sku:
                raise ValueError("selection discovery row omitted canonical SKU")
            if previous_sku is not None and sku <= previous_sku:
                raise ValueError("selection discovery rows are not globally unique and ordered")
            previous_sku = sku
            rows.append(row)
            if len(rows) == 10_001:
                break
        warnings.extend(cast(list[str], page.get("warnings", [])))
        ambiguities.extend(cast(list[str], page.get("ambiguities", [])))
        cursor = cast(str | None, page.get("next_cursor"))
        if cursor is not None:
            if cursor in seen_cursors:
                raise ValueError("selection discovery cursor did not advance")
            seen_cursors.add(cursor)
        if len(rows) == 10_001:
            break
        if page_no < len(pages) - 1 and cursor is None:
            raise ValueError("selection discovery continued after its terminal page")
        if page_no == len(pages) - 1 and cursor is not None:
            raise ValueError("selection discovery stopped before consuming its cursor")
    return {
        "selector": selector,
        "source_snapshot_id": snapshot_id,
        "snapshot_time": snapshot_time,
        "rows": rows,
        "warnings": list(dict.fromkeys(warnings)),
        "ambiguities": list(dict.fromkeys(ambiguities)),
        "limit_reached": len(rows) == 10_001,
    }


def _planner_result(snapshot: dict[str, Any]) -> tuple[dict[str, object], list[str]]:
    if snapshot.get("status") != "SUCCEEDED":
        raise ValueError("selection planner did not succeed")
    outputs = snapshot.get("outputs")
    result = outputs.get("result") if isinstance(outputs, dict) else None
    result = _agent_result_payload(result)
    if not isinstance(result, dict) or set(result) != {"status", "selector", "ambiguities"}:
        raise ValueError("selection planner output is invalid")
    ambiguities = result.get("ambiguities")
    if (
        not isinstance(ambiguities, list)
        or len(ambiguities) > 10
        or len(set(ambiguities)) != len(ambiguities)
        or not all(isinstance(item, str) and 1 <= len(item) <= 500 for item in ambiguities)
    ):
        raise ValueError("selection planner ambiguities are invalid")
    if result.get("status") != "READY":
        raise ValueError("selection request is ambiguous or unsupported")
    selector = InventorySelector.model_validate(result.get("selector")).model_dump(mode="json")
    return selector, cast(list[str], ambiguities)


def _agent_result_payload(value: object) -> object:
    if not isinstance(value, dict):
        return value
    envelope_fields = {"tenant_id", "status", "scope", "payload", "evidence", "issues"}
    if set(value) != envelope_fields:
        return value
    if value.get("status") != "COMPLETE" or value.get("issues") != []:
        raise ValueError("workflow returned a blocked business result")
    return value.get("payload")


def _timestamp(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("snapshot_time must be a string")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot_time must carry timezone")
    return value.astimezone(UTC)
