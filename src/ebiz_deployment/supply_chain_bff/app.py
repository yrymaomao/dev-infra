"""FastAPI surface for durable Supply Chain batches and activity SSE."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .config import BffSettings
from .contracts import (
    BatchCreateRequest,
    FeedbackRequest,
    FeedbackResponse,
    RuntimeProfilePayload,
)
from .cursor import CursorExpired, CursorInvalid, CursorSigner
from .dispatcher import BatchCoordinator
from .eta import EtaEstimator
from .level2_contracts import (
    ReportRunRequest,
    ScheduleCreate,
    SchedulePatch,
    SelectionPreviewRequest,
)
from .level2_repository import Level2Repository, ResourceConflict, ResourceNotReady
from .level2_worker import Level2Worker
from .policy import PolicyInvalid, validate_policy
from .report_export import report_export_header, report_export_row
from .repository import BatchRepository
from .runtime_client import RuntimeClient, RuntimeRequestError
from .selection_csv import MAX_CSV_BYTES, CsvFileError, parse_selection_csv

_TERMINAL_BATCH = frozenset({"SUCCEEDED", "BLOCKED", "FAILED", "PARTIAL", "CANCELLED"})
_TERMINAL_REPORT = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"})
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str


@dataclass(slots=True)
class BffContainer:
    settings: BffSettings
    repository: BatchRepository
    runtime: RuntimeClient
    coordinator: BatchCoordinator
    cursor: CursorSigner
    level2_repository: Level2Repository | None = None
    level2_worker: Level2Worker | None = None


def create_app(container: BffContainer) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(
            container.coordinator.run_forever(stop=stop),
            name="supply-chain-bff-coordinator",
        )
        level2_task = (
            asyncio.create_task(
                container.level2_worker.run_forever(stop=stop),
                name="supply-chain-level2-coordinator",
            )
            if container.level2_worker is not None
            else None
        )
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            if level2_task is not None:
                level2_task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if level2_task is not None:
                with suppress(asyncio.CancelledError):
                    await level2_task

    app = FastAPI(title="eBizHub Supply Chain BFF", version="0.1.4", lifespan=lifespan)

    def level2() -> Level2Repository:
        if not container.settings.level2_enabled:
            raise HTTPException(status_code=404, detail="Level 2 service is disabled")
        if container.level2_repository is None:
            raise HTTPException(status_code=503, detail="Level 2 service is unavailable")
        return container.level2_repository

    def principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or len(authorization) > 8192
        ):
            raise HTTPException(status_code=401, detail="authentication failed")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            claims = jwt.decode(
                token,
                container.settings.jwt_secret,
                algorithms=["HS256"],
                audience="agent-runtime",
                options={"require": ["exp", "aud", "sub", "tenant_id"]},
            )
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="authentication failed") from None
        tenant_id = claims.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 256:
            raise HTTPException(status_code=401, detail="authentication failed")
        return Principal(tenant_id=tenant_id)

    @app.get("/api/supply-chain/v2/runtime-profile")
    async def runtime_profile(
        _principal: Principal = Depends(principal),
    ) -> dict[str, object]:
        profile = container.settings.eta_profile
        return RuntimeProfilePayload(
            tenant_dispatch_concurrency=container.settings.tenant_dispatch_concurrency,
            eta_profile_version=profile.version,
            fixed_seconds=profile.fixed_seconds,
            per_item_seconds=profile.per_item_seconds,
            uncertainty_ratio=profile.uncertainty_ratio,
            async_start_enabled=container.settings.async_start_enabled,
            stream_enabled=container.settings.stream_enabled,
            activity_ui_enabled=container.settings.activity_ui_enabled,
            model_error_polish_enabled=container.settings.model_error_polish_enabled,
            level2_enabled=container.settings.level2_enabled,
            max_selected_skus=container.settings.max_selected_skus,
            bulk_batch_size=container.settings.bulk_batch_size,
            tenant_bulk_concurrency=container.settings.tenant_bulk_concurrency,
            global_bulk_concurrency=container.settings.global_bulk_concurrency,
        ).model_dump(mode="json")

    @app.post("/api/supply-chain/v2/analysis-batches", status_code=202)
    async def create_batch(
        body: BatchCreateRequest,
        current: Principal = Depends(principal),
    ) -> JSONResponse:
        eta = EtaEstimator(container.settings.eta_profile).estimate(item_count=len(body.skus))
        batch_id = await container.repository.create_batch(
            tenant_id=current.tenant_id,
            request=body,
            eta=eta,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )
        location = f"/api/supply-chain/v2/analysis-batches/{batch_id}"
        return JSONResponse(
            status_code=202,
            headers={"Location": location},
            content={
                "schema_version": "supply-chain.analysis-batch-accepted.v2",
                "batch_id": str(batch_id),
                "status": "ACCEPTED",
                "eta": {
                    "low_seconds": eta.low_seconds,
                    "high_seconds": eta.high_seconds,
                    "profile_version": eta.profile_version,
                    "dynamic": eta.dynamic,
                },
            },
        )

    @app.get("/api/supply-chain/v2/analysis-batches/{batch_id}")
    async def get_batch(
        batch_id: UUID,
        current: Principal = Depends(principal),
    ) -> dict[str, object]:
        snapshot = await container.repository.get_batch(
            tenant_id=current.tenant_id, batch_id=batch_id
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="batch not found")
        return snapshot

    @app.get("/api/supply-chain/v2/analysis-batches/{batch_id}/events")
    async def stream_batch_events(
        batch_id: UUID,
        request: Request,
        current: Principal = Depends(principal),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        if not container.settings.activity_ui_enabled:
            raise HTTPException(status_code=404, detail="activity stream is disabled")
        snapshot = await container.repository.get_batch(
            tenant_id=current.tenant_id, batch_id=batch_id
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="batch not found")
        sequences: dict[str, int] = {"_activity": 0}
        if last_event_id:
            decoded = container.cursor.verify(
                last_event_id,
                batch_id=str(batch_id),
                tenant_id=current.tenant_id,
            )
            sequences.update(decoded.sequences)

        async def generate() -> AsyncIterator[str]:
            last_heartbeat = asyncio.get_running_loop().time()
            while True:
                activities = await container.repository.list_activities(
                    tenant_id=current.tenant_id,
                    batch_id=batch_id,
                    after_stream_sequence=sequences.get("_activity", 0),
                )
                for activity in activities:
                    stream_sequence = int(activity["stream_sequence"])
                    sequences["_activity"] = stream_sequence
                    execution_id = activity.get("execution_id")
                    execution_sequence = activity.get("sequence")
                    if isinstance(execution_id, str) and isinstance(execution_sequence, int):
                        sequences[execution_id] = max(
                            sequences.get(execution_id, 0), execution_sequence
                        )
                    cursor = container.cursor.issue(
                        batch_id=str(batch_id),
                        tenant_id=current.tenant_id,
                        sequences=sequences,
                    )
                    data = json.dumps(activity, separators=(",", ":"), ensure_ascii=True)
                    yield f"id: {cursor}\nevent: activity\ndata: {data}\n\n"
                    if activity.get("state") in {"progress", "running"}:
                        await asyncio.sleep(1 / container.settings.activity_push_per_second)
                latest = await container.repository.get_batch(
                    tenant_id=current.tenant_id, batch_id=batch_id
                )
                if latest is None or latest.get("status") in _TERMINAL_BATCH:
                    return
                if await request.is_disconnected():
                    return
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat >= container.settings.heartbeat_seconds:
                    yield ": keepalive\n\n"
                    last_heartbeat = now
                await asyncio.sleep(container.settings.db_tail_seconds)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/supply-chain/v2/analysis-batches/{batch_id}/cancel")
    async def cancel_batch(
        batch_id: UUID,
        current: Principal = Depends(principal),
    ) -> dict[str, object]:
        targets = await container.repository.cancel_batch(
            tenant_id=current.tenant_id, batch_id=batch_id
        )
        if targets is None:
            raise HTTPException(status_code=404, detail="batch not found")
        for target in targets:
            try:
                await container.runtime.cancel(
                    authorization=container.coordinator.authorization_for_tenant(target.tenant_id),
                    execution_id=str(target.execution_id),
                )
            except RuntimeRequestError:
                continue
        return {"batch_id": str(batch_id), "status": "CANCEL_REQUESTED"}

    @app.post("/api/supply-chain/v2/submission-feedback")
    async def submission_feedback(
        body: FeedbackRequest,
        current: Principal = Depends(principal),
    ) -> dict[str, object]:
        if not container.settings.model_error_polish_enabled:
            raise HTTPException(status_code=404, detail="model error polishing is disabled")
        response = await container.runtime.model_feedback(
            authorization=container.coordinator.authorization_for_tenant(
                current.tenant_id,
                include_model_feedback=True,
            ),
            payload=body.model_dump(mode="json"),
        )
        return FeedbackResponse.model_validate(response).model_dump(mode="json")

    @app.post("/api/supply-chain/v2/selection-previews", status_code=202)
    async def create_selection_preview(
        body: SelectionPreviewRequest,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> JSONResponse:
        source_kind = "NATURAL_LANGUAGE" if body.natural_language is not None else "SELECTOR"
        preview_id = await repository.create_preview(
            tenant_id=current.tenant_id,
            client_request_id=body.client_request_id,
            source_kind=source_kind,
            request_payload=body.model_dump(mode="json"),
            selector=(body.selector.model_dump(mode="json") if body.selector else None),
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )
        return JSONResponse(
            status_code=202,
            headers={"Location": f"/api/supply-chain/v2/selection-previews/{preview_id}"},
            content={
                "schema_version": "supply-chain.selection-preview-accepted.v1",
                "preview_id": str(preview_id),
                "status": "PENDING",
            },
        )

    @app.get("/api/supply-chain/v2/selection-previews/{preview_id}")
    async def get_selection_preview(
        preview_id: UUID,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        preview = await repository.get_preview(
            tenant_id=current.tenant_id,
            preview_id=preview_id,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )
        if preview is None:
            raise HTTPException(status_code=404, detail="selection preview not found")
        return preview

    @app.post("/api/supply-chain/v2/selection-imports", status_code=202)
    async def import_selection_csv(
        request: Request,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> JSONResponse:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise CsvFileError("CSV file is required")
        content = await upload.read(MAX_CSV_BYTES + 1)
        if not isinstance(content, bytes):
            raise CsvFileError("CSV upload could not be read")
        parsed = parse_selection_csv(content)
        client_request_id = str(form.get("client_request_id") or uuid4().hex)
        preview_id = await repository.create_csv_preview(
            tenant_id=current.tenant_id,
            client_request_id=client_request_id,
            parsed=parsed,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )
        return JSONResponse(
            status_code=202,
            headers={"Location": f"/api/supply-chain/v2/selection-previews/{preview_id}"},
            content={
                "schema_version": "supply-chain.selection-preview-accepted.v1",
                "preview_id": str(preview_id),
                "status": "PENDING",
            },
        )

    @app.post("/api/supply-chain/v2/report-runs", status_code=202)
    async def create_report_run(
        body: ReportRunRequest,
        request: Request,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> JSONResponse:
        if container.level2_worker is None or not container.settings.level2_mq_enabled:
            raise HTTPException(status_code=503, detail="Level 2 report dispatch is unavailable")
        created = await repository.create_report(
            tenant_id=current.tenant_id,
            request=body,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
            trace_id=_request_id(request) or uuid4().hex,
        )
        return JSONResponse(
            status_code=202,
            headers={"Location": f"/api/supply-chain/v2/reports/{created.report_run_id}"},
            content={
                "schema_version": "supply-chain.report-run-accepted.v1",
                "report_run_id": str(created.report_run_id),
                "status": "ACCEPTED",
                "sku_count": created.sku_count,
                "batch_count": created.batch_count,
            },
        )

    @app.get("/api/supply-chain/v2/reports/{report_run_id}")
    async def get_report(
        report_run_id: UUID,
        item_offset: Annotated[int, Query(ge=0, le=9999)] = 0,
        item_limit: Annotated[int, Query(ge=1, le=200)] = 200,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        report = await repository.get_report(
            tenant_id=current.tenant_id,
            report_run_id=report_run_id,
            item_offset=item_offset,
            item_limit=item_limit,
        )
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        return report

    @app.get("/api/supply-chain/v2/reports")
    async def list_reports(
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> list[dict[str, object]]:
        return await repository.list_reports(tenant_id=current.tenant_id, limit=limit)

    @app.get("/api/supply-chain/v2/reports/{report_run_id}/export.csv")
    async def export_report(
        report_run_id: UUID,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> StreamingResponse:
        first_page = await repository.get_report(
            tenant_id=current.tenant_id,
            report_run_id=report_run_id,
            item_offset=0,
            item_limit=200,
        )
        if first_page is None:
            raise HTTPException(status_code=404, detail="report not found")
        if first_page["status"] not in _TERMINAL_REPORT:
            raise ResourceNotReady(
                "report export is available after the report reaches terminal state"
            )

        async def generate_report_csv() -> AsyncIterator[str]:
            yield report_export_header()
            page = first_page
            current_offset = 0
            while True:
                for item in page["items"]:
                    if isinstance(item, Mapping):
                        yield report_export_row(item)
                next_offset = page["next_item_offset"]
                if next_offset is None:
                    return
                if not isinstance(next_offset, int) or next_offset <= current_offset:
                    raise RuntimeError("report pagination did not advance")
                current_offset = next_offset
                next_page = await repository.get_report(
                    tenant_id=current.tenant_id,
                    report_run_id=report_run_id,
                    item_offset=current_offset,
                    item_limit=200,
                )
                if next_page is None or next_page["status"] not in _TERMINAL_REPORT:
                    raise RuntimeError("terminal report changed during export")
                page = next_page

        return StreamingResponse(
            generate_report_csv(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (f'attachment; filename="supply-chain-{report_run_id}.csv"'),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/supply-chain/v2/reports/{report_run_id}/events")
    async def stream_report_events(
        report_run_id: UUID,
        request: Request,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        if not container.settings.activity_ui_enabled:
            raise HTTPException(status_code=404, detail="activity stream is disabled")
        if (
            await repository.get_report_status(
                tenant_id=current.tenant_id,
                report_run_id=report_run_id,
            )
            is None
        ):
            raise HTTPException(status_code=404, detail="report not found")
        sequences = {"_report": 0}
        if last_event_id:
            decoded = container.cursor.verify(
                last_event_id,
                batch_id=str(report_run_id),
                tenant_id=current.tenant_id,
            )
            sequences.update(decoded.sequences)

        async def generate_report_events() -> AsyncIterator[str]:
            last_heartbeat = asyncio.get_running_loop().time()
            while True:
                activities = await repository.list_report_activities(
                    tenant_id=current.tenant_id,
                    report_run_id=report_run_id,
                    after_sequence=sequences["_report"],
                )
                for activity in activities:
                    sequences["_report"] = int(activity["stream_sequence"])
                    cursor = container.cursor.issue(
                        batch_id=str(report_run_id),
                        tenant_id=current.tenant_id,
                        sequences=sequences,
                    )
                    data = json.dumps(activity, separators=(",", ":"), ensure_ascii=True)
                    yield f"id: {cursor}\nevent: activity\ndata: {data}\n\n"
                    if activity.get("state") in {"progress", "running"}:
                        await asyncio.sleep(1 / container.settings.activity_push_per_second)
                latest_status = await repository.get_report_status(
                    tenant_id=current.tenant_id,
                    report_run_id=report_run_id,
                )
                if latest_status is None or latest_status in _TERMINAL_REPORT:
                    return
                if await request.is_disconnected():
                    return
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat >= container.settings.heartbeat_seconds:
                    yield ": keepalive\n\n"
                    last_heartbeat = now
                await asyncio.sleep(container.settings.db_tail_seconds)

        return StreamingResponse(
            generate_report_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/supply-chain/v2/reports/{report_run_id}/cancel", status_code=202)
    async def cancel_report(
        report_run_id: UUID,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        found = (
            await container.level2_worker.cancel_report(
                tenant_id=current.tenant_id,
                report_run_id=report_run_id,
            )
            if container.level2_worker is not None
            else await repository.cancel_report(
                tenant_id=current.tenant_id,
                report_run_id=report_run_id,
            )
        )
        if not found:
            raise HTTPException(status_code=404, detail="report not found")
        return {"report_run_id": str(report_run_id), "status": "CANCEL_REQUESTED"}

    @app.post("/api/supply-chain/v2/policies/validate")
    async def validate_policy_document(
        body: dict[str, Any],
        _current: Principal = Depends(principal),
    ) -> dict[str, object]:
        try:
            _digest, warnings = validate_policy(body)
        except PolicyInvalid as error:
            return {"valid": False, "errors": error.errors, "warnings": []}
        return {"valid": True, "errors": [], "warnings": list(warnings)}

    @app.post("/api/supply-chain/v2/policies", status_code=201)
    async def publish_policy(
        body: dict[str, Any],
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        return await repository.publish_policy(
            tenant_id=current.tenant_id,
            document=body,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )

    @app.get("/api/supply-chain/v2/policies")
    async def list_policies(
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> list[dict[str, object]]:
        return await repository.list_policies(tenant_id=current.tenant_id)

    @app.get("/api/supply-chain/v2/policies/{version}")
    async def get_policy(
        version: int,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        policy = await repository.get_policy(tenant_id=current.tenant_id, version=version)
        if policy is None:
            raise HTTPException(status_code=404, detail="policy not found")
        return policy

    @app.post("/api/supply-chain/v2/policies/{version}/activate")
    async def activate_policy(
        version: int,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        policy = await repository.activate_policy(
            tenant_id=current.tenant_id,
            version=version,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )
        if policy is None:
            raise HTTPException(status_code=404, detail="policy not found")
        return policy

    @app.post("/api/supply-chain/v2/schedules", status_code=201)
    async def create_schedule(
        body: ScheduleCreate,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        return await repository.create_schedule(
            tenant_id=current.tenant_id,
            request=body,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )

    @app.get("/api/supply-chain/v2/schedules")
    async def list_schedules(
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> list[dict[str, object]]:
        return await repository.list_schedules(tenant_id=current.tenant_id)

    @app.patch("/api/supply-chain/v2/schedules/{schedule_id}")
    async def update_schedule(
        schedule_id: UUID,
        body: SchedulePatch,
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        schedule = await repository.update_schedule(
            tenant_id=current.tenant_id,
            schedule_id=schedule_id,
            patch=body,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return schedule

    @app.post("/api/supply-chain/v2/schedules/{schedule_id}/run-now", status_code=202)
    async def run_schedule_now(
        schedule_id: UUID,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        current: Principal = Depends(principal),
        repository: Level2Repository = Depends(level2),
    ) -> dict[str, object]:
        if container.level2_worker is None or not container.settings.level2_mq_enabled:
            raise HTTPException(status_code=503, detail="Level 2 schedule worker is unavailable")
        if _REQUEST_ID.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Idempotency-Key is invalid")
        report_run_id = await repository.create_schedule_run(
            tenant_id=current.tenant_id,
            schedule_id=schedule_id,
            idempotency_key=idempotency_key,
            now=container.settings.snapshot_time_override or datetime.now(UTC),
        )
        if report_run_id is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return {
            "schema_version": "supply-chain.schedule-run-accepted.v1",
            "report_run_id": str(report_run_id),
            "status": "SELECTING",
        }

    @app.exception_handler(CursorInvalid)
    async def invalid_cursor(request: Request, _error: CursorInvalid) -> JSONResponse:
        return _safe_error(
            400,
            "CURSOR_INVALID",
            "The activity cursor is invalid.",
            request_id=_request_id(request),
        )

    @app.exception_handler(CursorExpired)
    async def expired_cursor(request: Request, _error: CursorExpired) -> JSONResponse:
        return _safe_error(
            410,
            "CURSOR_EXPIRED",
            "The activity cursor has expired.",
            request_id=_request_id(request),
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _error: RequestValidationError) -> JSONResponse:
        return _safe_error(
            422,
            "BATCH_REQUEST_INVALID",
            "The supply chain request is invalid.",
            request_id=_request_id(request),
        )

    @app.exception_handler(RuntimeRequestError)
    async def runtime_error(request: Request, error: RuntimeRequestError) -> JSONResponse:
        return _safe_error(
            error.status_code,
            error.error_code,
            error.safe_message,
            request_id=error.request_id or _request_id(request),
            phase=error.phase,
            category=error.category,
            retryable=error.retryable,
        )

    @app.exception_handler(ResourceConflict)
    async def resource_conflict(request: Request, error: ResourceConflict) -> JSONResponse:
        return _safe_error(
            409,
            "RESOURCE_CONFLICT",
            str(error),
            request_id=_request_id(request),
            category="conflict",
        )

    @app.exception_handler(ResourceNotReady)
    async def resource_not_ready(request: Request, error: ResourceNotReady) -> JSONResponse:
        return _safe_error(
            409,
            "RESOURCE_NOT_READY",
            str(error),
            request_id=_request_id(request),
            category="conflict",
            retryable=True,
        )

    @app.exception_handler(CsvFileError)
    async def invalid_csv(request: Request, error: CsvFileError) -> JSONResponse:
        return _safe_error(
            422,
            "CSV_FILE_INVALID",
            str(error),
            request_id=_request_id(request),
        )

    @app.exception_handler(PolicyInvalid)
    async def invalid_policy(request: Request, error: PolicyInvalid) -> JSONResponse:
        return _safe_error(
            422,
            "POLICY_INVALID",
            error.errors[0],
            request_id=_request_id(request),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        messages = {
            401: "Authentication is required.",
            403: "You do not have permission to run this analysis.",
            404: "The requested supply chain resource was not found.",
        }
        return _safe_error(
            error.status_code,
            f"BFF_HTTP_{error.status_code}",
            messages.get(error.status_code, "The supply chain request could not be completed."),
            request_id=_request_id(request),
            category="authorization" if error.status_code in {401, 403} else "request",
            retryable=error.status_code in {429, 503},
            headers=error.headers,
        )

    return app


def _request_id(request: Request) -> str | None:
    value = request.headers.get("x-request-id")
    return value if value is not None and _REQUEST_ID.fullmatch(value) else None


def _safe_error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    request_id: str | None = None,
    phase: str = "recovery",
    category: str = "validation",
    retryable: bool = False,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error_code": error_code,
            "phase": phase,
            "category": category,
            "retryable": retryable,
            "safe_message": message,
            "request_id": request_id,
        },
    )
