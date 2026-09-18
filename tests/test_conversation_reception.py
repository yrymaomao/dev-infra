"""Focused reception boundaries; PostgreSQL tests only use a dedicated *_test DB."""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from agent_runtime.payloads.memory import MemoryPayloadStore
from crm_fixtures import advised_result
from reception_fixtures import PROFILE_BUILDERS, PROFILE_IDS
from sqlalchemy import delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ebiz_deployment.openclaw_reception.conversation_contracts import OperationView
from ebiz_deployment.openclaw_reception.conversation_repository import (
    ConversationConflict,
    ConversationRepository,
    project_event,
    project_operation_update,
)
from ebiz_deployment.openclaw_reception.models import (
    Conversation,
    ConversationEvent,
    ConversationTurn,
)
from ebiz_deployment.supply_chain_bff.migration import upgrade


def test_events_reject_hidden_fields_and_bound_text():
    assert project_event("assistant.text.delta", {"text": "hello"}) == {"text": "hello"}
    with pytest.raises(ValueError):
        project_event("assistant.text.delta", {"text": "hello", "reasoning": "private"})
    with pytest.raises(ValueError):
        project_event("raw.mcp.body", {})
    with pytest.raises(ValueError):
        project_event("assistant.text.delta", {"text": "a" * 32769})
    with pytest.raises(ValueError):
        project_event("assistant.text.delta", {})


def test_public_operation_view_does_not_invent_runtime_execution_identity():
    operation = OperationView(operation_id="operation-1", state="completed")
    assert operation.execution_id is None


def test_public_operation_view_preserves_distinct_runtime_execution_identity():
    payload = project_event(
        "operation.updated",
        {
            "operation_id": "operation-1",
            "state": "completed",
            "execution_id": "execution-1",
        },
    )
    operation = OperationView(**payload)
    assert operation.model_dump(exclude_none=True) == {
        "operation_id": "operation-1",
        "state": "completed",
        "execution_id": "execution-1",
    }
    assert operation.operation_id != operation.execution_id


def test_operation_event_rejects_unknown_or_non_string_identity_fields():
    with pytest.raises(ValueError):
        project_event(
            "operation.updated",
            {"operation_id": "operation-1", "state": "running", "execution_id": 1},
        )
    with pytest.raises(ValueError):
        project_event(
            "operation.updated",
            {"operation_id": "operation-1", "state": "running", "session_id": "private"},
        )


def test_operation_event_preserves_bounded_runtime_safe_error():
    error = {
        "error_code": "EVIDENCE_PERSISTENCE_FAILED",
        "category": "permanent",
        "phase": "persistence",
        "retryable": False,
        "safe_message": "required evidence could not be persisted; the node fails closed",
        "trace_id": "trace-safe-1",
    }
    payload = project_event(
        "operation.updated",
        {"operation_id": "operation-1", "state": "error", "error": error},
    )
    assert payload["error"] == {key: value for key, value in error.items() if key != "trace_id"} | {
        "request_id": error["trace_id"]
    }
    operation = OperationView(**project_operation_update(None, payload))
    assert operation.error is not None
    assert operation.error.safe_message == error["safe_message"]
    assert operation.error.request_id == error["trace_id"]

    with pytest.raises(ValueError):
        project_event(
            "operation.updated",
            {
                "operation_id": "operation-1",
                "state": "error",
                "error": {**error, "raw_exception": "private"},
            },
        )


def test_result_observation_does_not_erase_known_runtime_execution_identity():
    current = project_operation_update(
        None,
        {
            "operation_id": "operation-1",
            "state": "running",
            "execution_id": "execution-1",
        },
    )
    updated = project_operation_update(
        current, {"operation_id": "operation-1", "state": "completed"}
    )
    assert updated == {
        "operation_id": "operation-1",
        "state": "completed",
        "execution_id": "execution-1",
    }


@pytest.mark.asyncio
async def test_report_projection_validates_v2_and_preserves_business_and_technical_outcomes():
    from ebiz_deployment.supply_chain_bff.conversation_result import project_analysis

    artifact = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts/supply-chain-level2/fixtures/report-batch-results.v2.valid.json"
        ).read_text(encoding="utf-8")
    )
    value = {
        "tenant_id": "tenant-a",
        "status": "COMPLETE",
        "scope": {},
        "issues": [],
        "evidence": [],
        "payload": {
            "result_artifact": artifact,
            "item_count": 3,
            "complete_count": 1,
            "blocked_count": 1,
            "failed_count": 1,
            "summary_artifact_ref": None,
            "risk_flags": [],
        },
    }
    result = await project_analysis(None, "tenant-a", value)
    assert result["report_run_id"] == artifact["report_run_id"]
    assert result["batch_id"] == artifact["batch_id"]
    assert result["item_offset"] == artifact["item_offset"]
    assert [row["result_status"] for row in result["items"]] == ["COMPLETE", "BLOCKED", "FAILED"]
    assert result["items"][0]["forecast"]["weeks"][0]["units_mean"] == 10
    assert result["items"][1]["business_issues"]
    assert result["items"][2]["runtime_error"]
    assert "payload://" not in json.dumps(result)
    with pytest.raises(ValueError, match="tenant mismatch"):
        await project_analysis(None, "tenant-b", value)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", PROFILE_IDS)
async def test_durable_idempotency_projection_recovery_and_isolation(profile_id):
    url = os.environ.get("SUPPLY_CHAIN_BFF_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Dedicated PostgreSQL test URL not configured")
    assert str(make_url(url).database).endswith("_test")
    await asyncio.to_thread(upgrade, url)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Only an isolated *_test fixture database: fence abandoned in-memory-store runs.
    assert str(make_url(url).database).endswith("_test")
    async with factory() as session, session.begin():
        await session.execute(
            update(ConversationTurn)
            .where(
                ConversationTurn.conversation_id.in_(
                    select(Conversation.id).where(
                        Conversation.tenant_id.like("test-%"), Conversation.principal_id == "alice"
                    )
                ),
                ConversationTurn.state.in_(["accepted", "running"]),
            )
            .values(state="interrupted")
        )
    store = MemoryPayloadStore(redacted_fields=frozenset(), inline_classifications=frozenset())
    profile = PROFILE_BUILDERS[profile_id]()
    other_profile = next(
        builder() for name, builder in PROFILE_BUILDERS.items() if name != profile_id
    )
    repository = ConversationRepository(factory, payload_store=store, profile=profile)
    foreign = ConversationRepository(factory, payload_store=store, profile=other_profile)
    tenant = "test-" + uuid4().hex
    cid = UUID((await repository.create_conversation(tenant, "alice"))["conversation_id"])
    request = str(uuid4())
    try:
        tid = await repository.submit(cid, tenant, "alice", request, "What is supply chain?")
        assert (
            await repository.submit(cid, tenant, "alice", request, "What is supply chain?") == tid
        )
        with pytest.raises(ConversationConflict, match="REQUEST_CONTENT_CONFLICT"):
            await repository.submit(cid, tenant, "alice", request, "different")
        with pytest.raises(ConversationConflict, match="CONVERSATION_BUSY"):
            await repository.submit(cid, tenant, "alice", str(uuid4()), "follow up")
        for other_tenant, other_user in [(tenant, "bob"), ("other", "alice")]:
            with pytest.raises(LookupError):
                await repository.snapshot(tid, other_tenant, other_user)
        # Another profile sharing the tables sees neither the conversation nor the turn.
        with pytest.raises(LookupError):
            await foreign.snapshot(tid, tenant, "alice")
        with pytest.raises(LookupError):
            await foreign.credential_context(f"turn:{tid}", str(tid))
        assert await foreign.claim("foreign-worker") is None
        claimed = await repository.claim("worker")
        assert claimed["id"] == tid
        context = await repository.credential_context(f"turn:{tid}", str(tid))
        artifact = json.loads(
            (
                Path(__file__).parents[1]
                / "contracts/supply-chain-level2/fixtures/report-batch-results.v2.valid.json"
            ).read_text(encoding="utf-8")
        )
        operation_id = "operation-g12"
        result = (
            advised_result(tenant=tenant)
            if profile_id == "crm"
            else {
                "tenant_id": tenant,
                "status": "COMPLETE",
                "scope": {},
                "issues": [],
                "evidence": [],
                "payload": {
                    "result_artifact": artifact,
                    "item_count": 3,
                    "complete_count": 1,
                    "blocked_count": 1,
                    "failed_count": 1,
                    "summary_artifact_ref": None,
                    "risk_flags": [],
                },
            }
        )
        events = [
            {"sequence": 1, "kind": "turn.accepted", "payload": {"run_id": str(tid)}},
            {"sequence": 2, "kind": "assistant.text.delta", "payload": {"text": "Hel"}},
            {"sequence": 3, "kind": "assistant.text.replaced", "payload": {"text": "Hello"}},
            {
                "sequence": 4,
                "kind": "operation.updated",
                "payload": {
                    "operation_id": operation_id,
                    "state": "completed",
                    "execution_id": "execution-g12",
                    "result": result,
                },
            },
            {
                "sequence": 5,
                "kind": "assistant.message.completed",
                "payload": {"text": "Hello world"},
            },
            {"sequence": 6, "kind": "turn.completed", "payload": {"run_id": str(tid)}},
        ]
        await repository.ingest(tid, "worker", tenant, events, "completed", 6)
        restarted = ConversationRepository(factory, payload_store=store, profile=profile)
        snapshot = await restarted.snapshot(tid, tenant, "alice")
        assert snapshot["reply"] == "Hello world"
        assert snapshot["state"] == "completed"
        assert snapshot["operations"][0]["operation_id"] == operation_id
        assert snapshot["analyses"][0]["operation_id"] == operation_id
        if profile_id == "crm":
            assert snapshot["analyses"][0]["outcome"] == "ADVISED"
            assert snapshot["analyses"][0]["advice"]["draft"]["body"].startswith("Hello")
            assert "content_ref" not in json.dumps(snapshot["analyses"])
        else:
            assert snapshot["analyses"][0]["report_run_id"] == artifact["report_run_id"]
            assert snapshot["analyses"][0]["batch_id"] == artifact["batch_id"]
            assert snapshot["analyses"][0]["item_offset"] == artifact["item_offset"]
        assert len(await restarted.events(tid, tenant, "alice", 4)) == 2
        next_id = await restarted.submit(cid, tenant, "alice", str(uuid4()), "Explain more")
        next_context = await restarted.credential_context(f"turn:{next_id}", str(next_id))
        assert context[2] == next_context[2], "model session persists across turns"
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                delete(ConversationEvent).where(
                    ConversationEvent.turn_id.in_(
                        select(ConversationTurn.id).where(ConversationTurn.conversation_id == cid)
                    )
                )
            )
            await session.execute(
                delete(ConversationTurn).where(ConversationTurn.conversation_id == cid)
            )
            await session.execute(delete(Conversation).where(Conversation.id == cid))
        await engine.dispose()
