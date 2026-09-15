"""Authenticated conversational API of one reception profile.

The router is generic; the profile supplies the prefix and the owner policy
decides who may hold a conversation once the bearer JWT has been verified by
the BFF's ``principal`` dependency: Supply Chain keeps its deployment-constant
tenant and principal, CRM takes both from the JWT the session exchange minted
and only fences the tenant (design decision D-1).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ebiz_deployment.supply_chain_bff.cursor import CursorExpired, CursorInvalid, CursorSigner

from .connector_contracts import OpenClawTurnRequest
from .conversation_contracts import (
    ConversationCreated,
    ConversationSnapshot,
    TurnAccepted,
    TurnEvent,
    TurnSnapshot,
)
from .conversation_repository import TERMINAL, ConversationConflict, ConversationRepository
from .profile import ReceptionProfile

T = TypeVar("T")
#: Receives the authenticated principal (``tenant_id``/``principal_id``) and
#: returns it, or raises ``HTTPException(403)``.
OwnerPolicy = Callable[[Any], Any]
_DENIED = "Conversation identity is not authorized"


def constant_owner(*, enabled: bool, tenant_id: str, principal_id: str) -> OwnerPolicy:
    """The Supply Chain rule: one deployment-constant tenant and principal."""

    def policy(current: Any) -> Any:
        if not enabled or current.tenant_id != tenant_id or current.principal_id != principal_id:
            raise HTTPException(403, _DENIED)
        return current

    return policy


def tenant_owner(*, enabled: bool, tenant_id: str) -> OwnerPolicy:
    """The CRM rule: the principal is whoever the verified JWT names, in this tenant."""

    def policy(current: Any) -> Any:
        if not enabled or current.tenant_id != tenant_id or not current.principal_id:
            raise HTTPException(403, _DENIED)
        return current

    return policy


def conversation_router(
    repository: ConversationRepository,
    profile: ReceptionProfile,
    signer: CursorSigner,
    authenticate: Callable[..., Any],
    owner_policy: OwnerPolicy,
) -> APIRouter:
    router = APIRouter(prefix=profile.api_prefix)

    def owner(current: Any = Depends(authenticate)) -> Any:
        return owner_policy(current)

    async def checked(awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except LookupError:
            raise HTTPException(404, "Conversation resource unavailable") from None
        except ConversationConflict as error:
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
            json.dumps([profile.profile_version, current.tenant_id, current.principal_id]).encode()
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


__all__ = ["OwnerPolicy", "constant_owner", "conversation_router", "tenant_owner"]
