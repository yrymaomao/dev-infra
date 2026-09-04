"""FastAPI surface for durable Supply Chain batches and activity SSE."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
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
from .repository import BatchRepository
from .runtime_client import RuntimeClient, RuntimeRequestError

_TERMINAL_BATCH = frozenset({"SUCCEEDED", "BLOCKED", "FAILED", "PARTIAL", "CANCELLED"})
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


def create_app(container: BffContainer) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(
            container.coordinator.run_forever(stop=stop),
            name="supply-chain-bff-coordinator",
        )
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="eBizHub Supply Chain BFF", version="0.1.3", lifespan=lifespan)

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
