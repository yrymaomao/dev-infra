"""Authenticated conversational API, separate from legacy synchronous ingress."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from .config import BffSettings
from .conversation_contracts import (
    ConversationCreated,
    ConversationSnapshot,
    TurnAccepted,
    TurnEvent,
    TurnSnapshot,
)
from .conversation_repository import TERMINAL, ConversationRepository
from .cursor import CursorExpired, CursorInvalid, CursorSigner
from .level2_contracts import OpenClawTurnRequest
from .level2_repository import ResourceConflict

T = TypeVar("T")


def conversation_router(
    repository: ConversationRepository,
    settings: BffSettings,
    signer: CursorSigner,
    authenticate: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/supply-chain/v2/openclaw")

    def owner(current: Any = Depends(authenticate)) -> Any:
        if (
            not settings.openclaw_enabled
            or current.tenant_id != settings.openclaw_tenant_id
            or current.principal_id != settings.openclaw_principal_id
        ):
            raise HTTPException(403, "Conversation identity is not authorized")
        return current

    async def checked(awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except LookupError:
            raise HTTPException(404, "Conversation resource unavailable") from None
        except ResourceConflict as error:
            raise HTTPException(409, str(error)) from None

    @router.post("/conversations", status_code=201, response_model=ConversationCreated)
    async def create(current: Any = Depends(owner)) -> dict[str, str]:
        return await repository.create_conversation(current.tenant_id, current.principal_id)

    @router.get("/conversations/{cid}", response_model=ConversationSnapshot)
    async def conversation(cid: UUID, current: Any = Depends(owner)) -> dict[str, Any]:
        return await checked(repository.conversation(cid, current.tenant_id, current.principal_id))

    @router.post("/conversations/{cid}/turns", status_code=202, response_model=TurnAccepted)
    async def submit(
        cid: UUID, body: OpenClawTurnRequest, current: Any = Depends(owner)
    ) -> dict[str, str]:
        if not body.prompt.strip():
            raise HTTPException(422, "Prompt must not be blank")
        tid = await checked(
            repository.submit(
                cid,
                current.tenant_id,
                current.principal_id,
                str(body.client_request_id),
                body.prompt,
            )
        )
        return {"turn_id": str(tid), "conversation_id": str(cid)}

    def binding(current: Any) -> str:
        return hashlib.sha256(
            json.dumps([current.tenant_id, current.principal_id]).encode()
        ).hexdigest()

    @router.get("/turns/{tid}", response_model=TurnSnapshot)
    async def snapshot(tid: UUID, current: Any = Depends(owner)) -> dict[str, Any]:
        result = await checked(repository.snapshot(tid, current.tenant_id, current.principal_id))
        result["cursor"] = signer.issue(
            batch_id=str(tid), tenant_id=binding(current), sequences={str(tid): result["sequence"]}
        )
        return result

    @router.get(
        "/turns/{tid}/events",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": (
                    "SSE replay and live events. Each activity frame contains a JSON TurnEvent "
                    "in data and a signed cursor in id. Resume using Last-Event-ID. "
                    "Keepalive comments contain no business event; "
                    "terminal events close the stream."
                ),
                "content": {
                    "text/event-stream": {
                        "schema": {"type": "string"},
                        "x-sse-data-schema": TurnEvent.model_json_schema(),
                    }
                },
            },
            400: {"description": "Invalid or tampered cursor."},
            410: {"description": "Expired cursor; recover from the turn snapshot."},
        },
    )
    async def events(
        tid: UUID,
        request: Request,
        last_event_id: str | None = Header(default=None),
        current: Any = Depends(owner),
    ) -> StreamingResponse:
        await checked(repository.position(tid, current.tenant_id, current.principal_id))
        after = 0
        if last_event_id:
            try:
                after = signer.verify(
                    last_event_id, batch_id=str(tid), tenant_id=binding(current)
                ).sequences.get(str(tid), 0)
            except CursorExpired:
                raise HTTPException(410, "Conversation cursor expired") from None
            except CursorInvalid:
                raise HTTPException(400, "Invalid conversation cursor") from None

        async def stream() -> AsyncIterator[str]:
            nonlocal after
            heartbeat = asyncio.get_running_loop().time()
            last_progress = 0.0
            while not await request.is_disconnected():
                rows = await repository.events(tid, current.tenant_id, current.principal_id, after)
                for event in rows:
                    if event["kind"] in {"tool.started", "operation.updated"}:
                        delay = 0.25 - (asyncio.get_running_loop().time() - last_progress)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        last_progress = asyncio.get_running_loop().time()
                    after = event["sequence"]
                    cursor = signer.issue(
                        batch_id=str(tid), tenant_id=binding(current), sequences={str(tid): after}
                    )
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: activity\ndata: {data}\n\n"
                    if event["kind"] in {"turn.completed", "turn.failed"}:
                        return
                if not rows:
                    state, sequence = await repository.position(
                        tid, current.tenant_id, current.principal_id
                    )
                    if state in TERMINAL and after >= sequence:
                        return
                if asyncio.get_running_loop().time() - heartbeat >= 15:
                    yield ": keepalive\n\n"
                    heartbeat = asyncio.get_running_loop().time()
                await asyncio.sleep(0.1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return router
