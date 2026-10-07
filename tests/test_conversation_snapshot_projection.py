"""Snapshot reconstruction must not fetch payloads that cannot affect its value."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from ebiz_deployment.openclaw_reception.conversation_repository import ConversationRepository

TURN = UUID("10000000-0000-4000-8000-000000000001")
CONVERSATION = UUID("10000000-0000-4000-8000-000000000002")


def event(sequence, kind):
    return SimpleNamespace(
        sequence=sequence, kind=kind, payload_ref=f"e{sequence}", payload_hash="hash"
    )


def repository(rows, payloads, *, owner_error=None):
    turn = SimpleNamespace(
        conversation_id=CONVERSATION,
        prompt_ref="prompt",
        request_id="request",
        state="completed",
        sequence=len(rows),
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=turn),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)),
    )

    @asynccontextmanager
    async def factory():
        yield session

    repo = object.__new__(ConversationRepository)
    repo._factory = factory
    repo._owner = AsyncMock(side_effect=owner_error)
    repo._profile = SimpleNamespace(finalize_reply=lambda text, analyses: text)

    async def load(tenant, ref, digest):
        assert tenant == "tenant-a"
        if ref == "prompt":
            return {"prompt": "SKU-1"}
        assert ref in payloads, f"unnecessary payload read: {ref}"
        value = payloads[ref]
        if isinstance(value, Exception):
            raise value
        return value

    repo._load = AsyncMock(side_effect=load)
    return repo, session


@pytest.mark.asyncio
async def test_completed_snapshot_skips_140_activity_payloads_and_superseded_text():
    rows = [
        event(i, "tool.started" if i % 2 else "assistant.message.started") for i in range(1, 141)
    ]
    rows += [
        event(141, "assistant.text.delta"),
        event(142, "operation.updated"),
        event(143, "operation.updated"),
        event(144, "assistant.message.completed"),
        event(145, "turn.completed"),
    ]
    repo, _ = repository(
        rows,
        {
            "e142": {"operation_id": "op-1", "execution_id": "ex-1", "state": "running"},
            "e143": {
                "operation_id": "op-1",
                "state": "completed",
                "analysis": {"report_run_id": "report-1", "items": [{"sku": "SKU-1"}]},
            },
            "e144": {"text": "Verified final analysis."},
        },
    )
    result = await repo.snapshot(TURN, "tenant-a", "alice")
    assert result["reply"] == "Verified final analysis."
    assert result["sequence"] == 145
    assert result["operations"][0]["execution_id"] == "ex-1"
    assert result["operations"][0]["state"] == "completed"
    assert result["analyses"][0]["report_run_id"] == "report-1"
    assert repo._load.await_count == 4
    repo._owner.assert_awaited_once_with(_, CONVERSATION, "tenant-a", "alice")


@pytest.mark.asyncio
async def test_latest_replacement_keeps_following_deltas_without_loading_superseded_text():
    repo, _ = repository(
        [
            event(1, "assistant.text.delta"),
            event(2, "assistant.text.replaced"),
            event(3, "assistant.text.delta"),
        ],
        {"e2": {"text": "New"}, "e3": {"text": " answer"}},
    )
    assert (await repo.snapshot(TURN, "tenant-a", "alice"))["reply"] == "New answer"
    assert repo._load.await_count == 3


@pytest.mark.asyncio
async def test_unfinished_text_without_replacement_still_accumulates_all_deltas():
    repo, _ = repository(
        [event(1, "assistant.text.delta"), event(2, "assistant.text.delta")],
        {"e1": {"text": "Hello"}, "e2": {"text": " there"}},
    )
    assert (await repo.snapshot(TURN, "tenant-a", "alice"))["reply"] == "Hello there"


@pytest.mark.asyncio
async def test_failed_snapshot_keeps_safe_error_and_known_operation_identity():
    error = {"code": "ASSISTANT_TURN_FAILED", "safe_message": "Final explanation unavailable."}
    repo, _ = repository(
        [event(1, "operation.updated"), event(2, "turn.failed")],
        {"e1": {"operation_id": "op-1", "execution_id": "ex-1", "state": "completed"}, "e2": error},
    )
    result = await repo.snapshot(TURN, "tenant-a", "alice")
    assert result["error"] == error
    assert result["operations"][0]["execution_id"] == "ex-1"


@pytest.mark.asyncio
async def test_owner_denial_precedes_any_payload_read():
    repo, session = repository(
        [event(1, "assistant.message.completed")], {}, owner_error=LookupError("denied")
    )
    with pytest.raises(LookupError, match="denied"):
        await repo.snapshot(TURN, "tenant-a", "other-user")
    repo._load.assert_not_awaited()
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_visible_payload_failure_is_not_hidden():
    repo, _ = repository(
        [event(1, "assistant.message.completed")], {"e1": RuntimeError("missing required payload")}
    )
    with pytest.raises(RuntimeError, match="missing required payload"):
        await repo.snapshot(TURN, "tenant-a", "alice")
