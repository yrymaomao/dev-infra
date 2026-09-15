"""Durable turn admission and event projection, using existing restricted payload storage."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .conversation_models import Conversation, ConversationEvent, ConversationTurn
from .level2_repository import Level2Repository, ResourceConflict

TERMINAL = frozenset({"completed", "failed", "interrupted"})
KINDS = frozenset(
    {
        "turn.accepted",
        "assistant.message.started",
        "assistant.text.delta",
        "assistant.text.replaced",
        "tool.started",
        "operation.updated",
        "assistant.message.completed",
        "turn.completed",
        "turn.failed",
    }
)


def project_event(kind: str, payload: object) -> dict[str, Any]:
    if kind not in KINDS or not isinstance(payload, dict):
        raise ValueError("Invalid conversation event")
    if kind == "operation.updated":
        allowed = {"operation_id", "state", "execution_id", "error"}
        if (
            not {"operation_id", "state"}.issubset(payload)
            or not set(payload).issubset(allowed)
            or any(
                not isinstance(payload.get(key), str)
                for key in ("operation_id", "state")
            )
            or (
                "execution_id" in payload
                and not isinstance(payload.get("execution_id"), str)
            )
        ):
            raise ValueError("Invalid conversation payload")
        projected = {
            key: value for key, value in payload.items() if key != "error"
        }
        if "error" in payload:
            projected["error"] = _project_runtime_error(payload["error"])
        return projected
    allowed = {
        "assistant.text.delta": {"text"},
        "assistant.text.replaced": {"text"},
        "assistant.message.completed": {"text"},
        "assistant.message.started": set(),
        "turn.accepted": {"run_id"},
        "turn.completed": {"run_id"},
        "tool.started": {"tool"},
        "turn.failed": {"code", "safe_message"},
    }[kind]
    required = allowed - ({"execution_id"} if kind == "operation.updated" else set())
    if (
        not required.issubset(payload)
        or not set(payload).issubset(allowed)
        or any(not isinstance(v, str) for v in payload.values())
    ):
        raise ValueError("Invalid conversation payload")
    if any(len(v) > (32768 if k == "text" else 512) for k, v in payload.items()):
        raise ValueError("Conversation payload exceeds bound")
    return payload


def _project_runtime_error(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Invalid Runtime error")
    required = {
        "error_code",
        "category",
        "phase",
        "retryable",
        "safe_message",
        "trace_id",
    }
    allowed = required | {"workflow_code", "node_id", "provider_code", "details_ref"}
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise ValueError("Invalid Runtime error")
    if any(
        not isinstance(value[key], str)
        for key in ("error_code", "category", "phase", "safe_message", "trace_id")
    ) or not isinstance(value["retryable"], bool):
        raise ValueError("Invalid Runtime error")
    if value["category"] not in {
        "validation",
        "authorization",
        "configuration",
        "transient",
        "permanent",
        "conflict",
    } or value["phase"] not in {
        "validation",
        "routing",
        "invocation",
        "persistence",
        "governance",
        "recovery",
    }:
        raise ValueError("Invalid Runtime error")
    limits = {"error_code": 128, "safe_message": 1024, "trace_id": 512}
    if any(not value[key] or len(value[key]) > limit for key, limit in limits.items()):
        raise ValueError("Invalid Runtime error")
    return {
        "error_code": value["error_code"],
        "category": value["category"],
        "phase": value["phase"],
        "retryable": value["retryable"],
        "safe_message": value["safe_message"],
        "request_id": value["trace_id"],
    }


def project_operation_update(
    current: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """Merge status/result observations without erasing a known Runtime execution identity."""

    execution_id = payload.get("execution_id")
    if execution_id is None and current is not None:
        execution_id = current.get("execution_id")
    result = {
        "operation_id": payload["operation_id"],
        "state": payload["state"],
        "execution_id": execution_id,
    }
    error = payload.get("error")
    if error is None and current is not None:
        error = current.get("error")
    if error is not None:
        result["error"] = error
    return result


class ConversationRepository(Level2Repository):
    async def position(self, tid: UUID, tenant: str, user: str) -> tuple[str, int]:
        """Read stream completion without reloading every historical message payload."""
        async with self._factory() as session:
            turn = await session.get(ConversationTurn, tid)
            if not turn:
                raise LookupError("Turn unavailable")
            await self._owner(session, turn.conversation_id, tenant, user)
            return turn.state, turn.sequence

    async def create_conversation(self, tenant: str, user: str) -> dict[str, str]:
        row = Conversation(
            id=uuid4(),
            tenant_id=tenant,
            principal_id=user,
            profile_version="supply-chain-reception.v1",
            session_id=str(uuid4()),
        )
        async with self._factory() as session, session.begin():
            session.add(row)
        return {"conversation_id": str(row.id), "profile_version": row.profile_version}

    async def _owner(
        self, session: AsyncSession, cid: UUID, tenant: str, user: str, *, lock: bool = False
    ) -> Conversation:
        query = select(Conversation).where(
            Conversation.id == cid,
            Conversation.tenant_id == tenant,
            Conversation.principal_id == user,
        )
        row = await session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise LookupError("Conversation unavailable")
        return row

    async def submit(self, cid: UUID, tenant: str, user: str, request_id: str, prompt: str) -> UUID:
        if not prompt.strip() or len(prompt) > 8192:
            raise ValueError("Invalid prompt")
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        async with self._factory() as session, session.begin():
            await self._owner(session, cid, tenant, user, lock=True)
            old = await session.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.conversation_id == cid,
                    ConversationTurn.request_id == request_id,
                )
            )
            if old:
                if old.prompt_hash != digest:
                    raise ResourceConflict("REQUEST_CONTENT_CONFLICT")
                return old.id
            active = await session.scalar(
                select(ConversationTurn.id)
                .where(
                    ConversationTurn.conversation_id == cid, ConversationTurn.state.not_in(TERMINAL)
                )
                .limit(1)
            )
            if active:
                raise ResourceConflict("CONVERSATION_BUSY")
            payload = await self._stage(tenant, {"prompt": prompt})
            await self._commit_staged(tenant, payload)
            row = ConversationTurn(
                id=uuid4(),
                conversation_id=cid,
                request_id=request_id,
                prompt_hash=digest,
                prompt_ref=payload.payload_ref,
                state="accepted",
                sequence=0,
            )
            session.add(row)
            return row.id

    async def credential_context(self, selector: str, run_id: str) -> tuple[str, str, str, UUID]:
        prefix = "reconcile:" if selector.startswith("reconcile:") else "turn:"
        tid = UUID(selector.removeprefix(prefix))
        if selector != f"{prefix}{tid}" or (prefix == "turn:" and run_id != str(tid)):
            raise LookupError("Turn binding denied")
        UUID(run_id)
        async with self._factory() as session:
            pair = (
                await session.execute(
                    select(ConversationTurn, Conversation)
                    .join(Conversation, Conversation.id == ConversationTurn.conversation_id)
                    .where(ConversationTurn.id == tid)
                )
            ).one_or_none()
        if pair is None or pair[0].state in TERMINAL:
            raise LookupError("Turn binding unavailable")
        return pair[1].tenant_id, pair[1].principal_id, pair[1].session_id, tid

    async def snapshot(self, tid: UUID, tenant: str, user: str) -> dict[str, Any]:
        async with self._factory() as session:
            turn = await session.get(ConversationTurn, tid)
            if not turn:
                raise LookupError("Turn unavailable")
            await self._owner(session, turn.conversation_id, tenant, user)
            rows = (
                await session.scalars(
                    select(ConversationEvent)
                    .where(ConversationEvent.turn_id == tid)
                    .order_by(ConversationEvent.sequence)
                )
            ).all()
        prompt = await self._load(tenant, turn.prompt_ref, None)
        message = ""
        operations: dict[str, Any] = {}
        analyses: dict[str, Any] = {}
        error = None
        for row in rows:
            p = await self._load(tenant, row.payload_ref, row.payload_hash)
            if row.kind == "assistant.text.delta":
                message += p["text"]
            elif row.kind in {"assistant.text.replaced", "assistant.message.completed"}:
                message = p["text"]
            elif row.kind == "operation.updated":
                operations[p["operation_id"]] = project_operation_update(
                    operations.get(p["operation_id"]), p
                )
                if "analysis" in p:
                    analyses[p["operation_id"]] = {
                        "operation_id": p["operation_id"],
                        **p["analysis"],
                    }
            elif row.kind == "turn.failed":
                error = p
        if analyses:
            from .conversation_result import qualify_unowned_currency_symbols

            message = qualify_unowned_currency_symbols(message, list(analyses.values()))
        return {
            "turn_id": str(tid),
            "client_request_id": turn.request_id,
            "conversation_id": str(turn.conversation_id),
            "message_id": str(tid),
            "state": turn.state,
            "sequence": turn.sequence,
            "prompt": prompt["prompt"],
            "reply": message,
            "operations": list(operations.values()),
            "analyses": list(analyses.values()),
            "error": error,
        }

    async def conversation(self, cid: UUID, tenant: str, user: str) -> dict[str, Any]:
        async with self._factory() as session:
            await self._owner(session, cid, tenant, user)
            ids = (
                await session.scalars(
                    select(ConversationTurn.id)
                    .where(ConversationTurn.conversation_id == cid)
                    .order_by(ConversationTurn.created_at, ConversationTurn.id)
                )
            ).all()
        return {
            "conversation_id": str(cid),
            "turns": [await self.snapshot(tid, tenant, user) for tid in ids],
        }

    async def events(self, tid: UUID, tenant: str, user: str, after: int) -> list[dict[str, Any]]:
        async with self._factory() as session:
            turn = await session.get(ConversationTurn, tid)
            if not turn:
                raise LookupError("Turn unavailable")
            await self._owner(session, turn.conversation_id, tenant, user)
            rows = (
                await session.scalars(
                    select(ConversationEvent)
                    .where(ConversationEvent.turn_id == tid, ConversationEvent.sequence > after)
                    .order_by(ConversationEvent.sequence)
                    .limit(128)
                )
            ).all()
        return [
            {
                "sequence": row.sequence,
                "kind": row.kind,
                "message_id": str(tid),
                "payload": {
                    key: part
                    for key, part in (
                        await self._load(tenant, row.payload_ref, row.payload_hash)
                    ).items()
                    if key != "analysis"
                },
            }
            for row in rows
        ]

    async def claim(self, owner: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(ConversationTurn)
                .where(
                    ConversationTurn.state.not_in(TERMINAL),
                    or_(ConversationTurn.lease_until.is_(None), ConversationTurn.lease_until < now),
                )
                .order_by(ConversationTurn.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.lease_owner, row.lease_until = owner, now + timedelta(seconds=15)
            c = await session.get(Conversation, row.conversation_id)
            if c is None:
                raise LookupError("Conversation unavailable")
            value: dict[str, Any] = {
                "id": row.id,
                "tenant": c.tenant_id,
                "sequence": row.sequence,
                "prompt_ref": row.prompt_ref,
            }
        value["prompt"] = (await self._load(value["tenant"], value["prompt_ref"], None))["prompt"]
        return value

    async def ingest(
        self,
        tid: UUID,
        owner: str,
        tenant: str,
        events: list[dict[str, Any]],
        state: str,
        total: int,
    ) -> None:
        staged = []
        for event in events:
            raw = dict(event["payload"])
            result = raw.pop("result", None) if event["kind"] == "operation.updated" else None
            public = project_event(event["kind"], raw)
            if result is not None:
                from .conversation_result import project_analysis

                public["analysis"] = await project_analysis(self, tenant, result)
            payload = await self._stage(tenant, public)
            await self._commit_staged(tenant, payload)
            staged.append((event, payload))
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(ConversationTurn).where(ConversationTurn.id == tid).with_for_update()
            )
            if row is None or row.lease_owner != owner:
                raise ResourceConflict("TURN_LEASE_LOST")
            for event, payload in staged:
                if event["sequence"] <= row.sequence:
                    continue
                if event["sequence"] != row.sequence + 1:
                    raise ValueError("Non-contiguous turn events")
                session.add(
                    ConversationEvent(
                        turn_id=tid,
                        sequence=event["sequence"],
                        kind=event["kind"],
                        payload_ref=payload.payload_ref,
                        payload_hash=payload.payload_hash,
                    )
                )
                row.sequence = event["sequence"]
            if state in TERMINAL and row.sequence == total:
                if not staged or staged[-1][0]["kind"] not in {"turn.completed", "turn.failed"}:
                    raise ValueError("Missing terminal event")
                row.state = state
            else:
                row.state = "running"
            row.lease_owner = None
            row.lease_until = datetime.now(UTC) + timedelta(milliseconds=500)
