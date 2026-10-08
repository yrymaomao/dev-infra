from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_conversation_result_isolation import result_for

from ebiz_deployment.openclaw_reception.models import Conversation
from ebiz_deployment.supply_chain_bff.conversation_reports import (
    SupplyChainConversationRepository,
)
from ebiz_deployment.supply_chain_bff.level2_models import ReportBatch, ReportRun
from ebiz_deployment.supply_chain_bff.level2_repository import Level2Repository


class Session:
    def __init__(self, conversation):
        self.rows = {(Conversation, conversation.id): conversation}
        self.added = []

    async def get(self, model, key):
        return self.rows.get((model, key))

    def add(self, row):
        self.added.append(row)
        self.rows[(type(row), row.id)] = row

    async def flush(self):
        pass

    async def scalar(self, query):
        return next((v for (m, _), v in self.rows.items() if m is ReportRun), None)

    async def scalars(self, query):
        return SimpleNamespace(
            all=lambda: [v for (m, _), v in self.rows.items() if m is ReportBatch]
        )


def fixture():
    cid, tid = uuid4(), uuid4()
    conversation = SimpleNamespace(
        id=cid,
        tenant_id="tenant-a",
        principal_id="alice",
        profile_version="supply-chain-reception.v1",
    )
    repo = object.__new__(SupplyChainConversationRepository)
    repo._profile = SimpleNamespace(profile_version=conversation.profile_version)
    repo._credential_reference = lambda tenant, user: f"opaque:{tenant}:{user}"
    repo._stage = AsyncMock(
        return_value=SimpleNamespace(payload_ref="payload://report", payload_hash="a" * 64)
    )
    repo._commit_staged = AsyncMock()
    return repo, Session(conversation), SimpleNamespace(id=tid, conversation_id=cid)


async def prepared(repo, value=None):
    if value is None:
        value = result_for("CHAT")
        value["payload"]["result_artifact"]["item_offset"] = 0
        for ordinal, item in enumerate(value["payload"]["result_artifact"]["items"]):
            item["ordinal"] = ordinal
    return await repo._prepare_result(
        "tenant-a",
        value,
        {"payload": {"operation_id": str(uuid4()), "execution_id": str(uuid4())}},
    )


@pytest.mark.asyncio
async def test_completed_chat_materializes_readable_report_without_dispatch():
    repo, session, turn = fixture()
    publication = await prepared(repo)
    await repo._commit_result(session, turn, publication)
    assert [type(row) for row in session.added] == [ReportRun, ReportBatch]
    report, batch = session.added
    assert report.owner_uid == "alice" and report.credential_ref == "opaque:tenant-a:alice"
    assert report.status == "PARTIAL" and report.batch_count == 1
    assert (report.complete_count, report.blocked_count, report.failed_count) == (1, 1, 1)
    assert batch.runtime_execution_id == publication.execution_id

    @asynccontextmanager
    async def factory():
        yield session

    reader = object.__new__(Level2Repository)
    reader._factory = factory
    reader._load = AsyncMock(return_value=publication.artifact)
    snapshot = await reader.get_report(
        tenant_id="tenant-a", owner_uid="alice", report_run_id=report.id
    )
    assert snapshot["report_run_id"] == publication.artifact["report_run_id"]
    assert snapshot["items"] == publication.artifact["items"]
    assert snapshot["batches"][0]["execution_id"] == str(publication.execution_id)


@pytest.mark.asyncio
async def test_replay_keeps_the_same_report_and_result():
    repo, session, turn = fixture()
    publication = await prepared(repo)
    await repo._commit_result(session, turn, publication)
    await repo._commit_result(session, turn, publication)
    assert len(session.added) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["tenant", "user", "execution", "hash"])
async def test_existing_report_cannot_be_rebound(mismatch):
    repo, session, turn = fixture()
    publication = await prepared(repo)
    await repo._commit_result(session, turn, publication)
    report, batch = session.added
    if mismatch == "tenant":
        report.tenant_id = "tenant-b"
    elif mismatch == "user":
        report.owner_uid = "bob"
    elif mismatch == "execution":
        batch.runtime_execution_id = uuid4()
    else:
        batch.result_hash = "b" * 64
    with pytest.raises(ValueError, match="identity conflict"):
        await repo._commit_result(session, turn, publication)
    assert len(session.added) == 2


@pytest.mark.asyncio
async def test_foreign_result_rejected_before_storing_payload():
    repo, session, turn = fixture()
    value = result_for("FOREIGN")
    value["tenant_id"] = "tenant-b"
    with pytest.raises(ValueError, match="tenant mismatch"):
        await prepared(repo, value)
    repo._stage.assert_not_awaited()
    assert not session.added
