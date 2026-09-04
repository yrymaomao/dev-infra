from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from agent_runtime.payloads.memory import MemoryPayloadStore
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ebiz_deployment.supply_chain_bff.activity import ActivityProjection
from ebiz_deployment.supply_chain_bff.level2_contracts import CsvSelectionRow, ReportRunRequest
from ebiz_deployment.supply_chain_bff.level2_models import (
    ReportBatch,
    ReportInbox,
    ReportOutbox,
)
from ebiz_deployment.supply_chain_bff.level2_repository import (
    ActiveReportExecution,
    Level2Repository,
)
from ebiz_deployment.supply_chain_bff.migration import upgrade
from ebiz_deployment.supply_chain_bff.report_mq import ReportBatchMessage
from ebiz_deployment.supply_chain_bff.runtime_client import RuntimeStartResult
from ebiz_deployment.supply_chain_bff.selection_csv import CsvSelection

RESULT_FIXTURE = (
    Path(__file__).parents[1]
    / "contracts"
    / "supply-chain-level2"
    / "fixtures"
    / "report-batch-results.valid.json"
)


@pytest.fixture(scope="module")
def level2_database_url() -> str:
    database_url = os.environ.get("SUPPLY_CHAIN_BFF_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SUPPLY_CHAIN_BFF_TEST_DATABASE_URL is not configured")
    database = str(make_url(database_url).database or "")
    if not database.endswith("_test"):
        pytest.fail("refusing destructive Level 2 test outside a *_test database")
    sync_url = make_url(database_url).set(drivername="postgresql+psycopg")
    sync_engine = create_engine(sync_url)
    with sync_engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS supply_chain_bff CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    sync_engine.dispose()
    upgrade(database_url)
    return database_url


@pytest.mark.asyncio
async def test_10000_skus_freeze_to_exactly_50_durable_idempotent_batches(
    level2_database_url: str,
) -> None:
    engine = create_async_engine(level2_database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE supply_chain_bff.report_activity, "
                "supply_chain_bff.report_inbox, supply_chain_bff.report_outbox, "
                "supply_chain_bff.report_batch, supply_chain_bff.report_run, "
                "supply_chain_bff.report_schedule, supply_chain_bff.policy_version, "
                "supply_chain_bff.selection_preview CASCADE"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = MemoryPayloadStore(redacted_fields=frozenset(), inline_classifications=frozenset())
    repository = Level2Repository(factory, payload_store=store)
    tenant_id = f"level2-10000-{uuid4().hex}"
    now = datetime.now(UTC)
    rows = tuple(CsvSelectionRow(row=index + 2, sku=f"SKU-{index:05d}") for index in range(10_000))
    preview_id = await repository.create_csv_preview(
        tenant_id=tenant_id,
        client_request_id="csv-10000",
        parsed=CsvSelection(rows=rows, errors=(), input_row_count=10_000),
        now=now,
    )
    request = ReportRunRequest(
        selection_preview_id=preview_id,
        client_request_id="report-10000",
    )
    created = await repository.create_report(
        tenant_id=tenant_id,
        request=request,
        now=now,
        trace_id="trace-10000",
    )
    replay = await repository.create_report(
        tenant_id=tenant_id,
        request=request,
        now=now,
        trace_id="trace-retry",
    )
    assert replay == created
    assert created.sku_count == 10_000
    assert created.batch_count == 50

    async with factory() as session:
        batches = (
            await session.scalars(
                select(ReportBatch)
                .where(ReportBatch.tenant_id == tenant_id)
                .order_by(ReportBatch.batch_no)
            )
        ).all()
        outbox_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ReportOutbox)
                .where(ReportOutbox.tenant_id == tenant_id)
            )
            or 0
        )
    assert len(batches) == 50
    assert outbox_count == 50
    assert [batch.item_offset for batch in batches] == list(range(0, 10_000, 200))
    assert {batch.item_count for batch in batches} == {200}

    snapshot = await repository.get_report(
        tenant_id=tenant_id,
        report_run_id=created.report_run_id,
    )
    assert snapshot is not None
    assert snapshot["risk_flags"] == [
        "POLICY_DEFAULTED",
        "CAPITAL_COST_DEFAULTED_1_PERCENT",
    ]
    listed = await repository.list_reports(tenant_id=tenant_id, limit=100)
    assert [item["report_run_id"] for item in listed] == [str(created.report_run_id)]

    first_claim = await repository.claim_outbox(worker_id="publisher-before-crash", now=now)
    assert first_claim is not None
    async with factory() as session, session.begin():
        await session.execute(
            update(ReportOutbox)
            .where(ReportOutbox.event_id == first_claim.event_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
    recovered = await repository.claim_outbox(worker_id="publisher-after-crash", now=now)
    assert recovered is not None
    assert recovered.event_id == first_claim.event_id
    await repository.mark_outbox_published(recovered, now=now)

    message = ReportBatchMessage.model_validate(recovered.payload)
    delivery = await repository.claim_report_delivery(
        message=message,
        worker_id="consumer-before-crash",
        now=now,
        tenant_concurrency=2,
        global_concurrency=8,
    )
    assert delivery is not None
    execution_id = UUID("00000000-0000-4000-8000-000000000099")
    await repository.record_report_start(
        delivery,
        RuntimeStartResult(
            mode="async",
            execution_id=str(execution_id),
            root_execution_id=str(execution_id),
            session_id=str(UUID("00000000-0000-4000-8000-000000000098")),
            snapshot={"status": "CREATED"},
        ),
        now=now,
    )
    duplicate = await repository.claim_report_delivery(
        message=message,
        worker_id="consumer-redelivery",
        now=now,
        tenant_concurrency=2,
        global_concurrency=8,
    )
    assert duplicate is None
    async with factory() as session:
        inbox_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ReportInbox)
                .where(ReportInbox.event_id == recovered.event_id)
            )
            or 0
        )
    assert inbox_count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_batch_result_is_authorized_validated_and_paginated(
    level2_database_url: str,
) -> None:
    engine = create_async_engine(level2_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = MemoryPayloadStore(redacted_fields=frozenset(), inline_classifications=frozenset())
    repository = Level2Repository(factory, payload_store=store)
    tenant_id = f"level2-results-{uuid4().hex}"
    now = datetime.now(UTC)
    rows = tuple(CsvSelectionRow(row=index + 2, sku=f"SKU-RESULT-{index}") for index in range(3))
    preview_id = await repository.create_csv_preview(
        tenant_id=tenant_id,
        client_request_id="csv-results",
        parsed=CsvSelection(rows=rows, errors=(), input_row_count=3),
        now=now,
    )
    created = await repository.create_report(
        tenant_id=tenant_id,
        request=ReportRunRequest(
            selection_preview_id=preview_id,
            client_request_id="report-results",
        ),
        now=now,
        trace_id="trace-results",
    )
    async with factory() as session:
        batch = await session.scalar(
            select(ReportBatch).where(
                ReportBatch.tenant_id == tenant_id,
                ReportBatch.report_run_id == created.report_run_id,
            )
        )
        outbox = await session.scalar(
            select(ReportOutbox).where(
                ReportOutbox.tenant_id == tenant_id,
                ReportOutbox.report_run_id == created.report_run_id,
            )
        )
    assert batch is not None
    assert outbox is not None
    delivery = await repository.claim_report_delivery(
        message=ReportBatchMessage.model_validate(outbox.payload),
        worker_id="integration-worker",
        now=now,
        tenant_concurrency=2,
        global_concurrency=8,
    )
    assert delivery is not None
    execution_id = uuid4()
    artifact = json.loads(RESULT_FIXTURE.read_text(encoding="utf-8"))
    artifact["report_run_id"] = str(created.report_run_id)
    artifact["batch_id"] = str(batch.id)
    artifact["item_offset"] = 0
    for ordinal, item in enumerate(artifact["items"]):
        item["ordinal"] = ordinal
    staged = await store.put_exact_restricted(
        tenant_id=tenant_id,
        payload=artifact,
        required_permission="supply-chain:level2",
    )
    assert staged.payload_ref is not None
    await store.ensure_committed(
        tenant_id=tenant_id,
        payload_ref=staged.payload_ref,
        payload_hash=staged.payload_hash,
        size_bytes=staged.size_bytes,
        content_type=staged.content_type,
        classification=staged.classification,
        required_permission=staged.required_permission,
    )
    await repository.record_report_start(
        delivery,
        RuntimeStartResult(
            mode="async",
            execution_id=str(execution_id),
            root_execution_id=str(execution_id),
            session_id=str(uuid4()),
            snapshot={"status": "CREATED"},
        ),
        now=now,
    )
    target = ActiveReportExecution(
        tenant_id=tenant_id,
        report_run_id=created.report_run_id,
        batch_id=batch.id,
        execution_id=execution_id,
        sequence=0,
        item_offset=0,
        item_count=3,
    )
    await repository.record_report_runtime_event(
        target=target,
        projection=ActivityProjection(
            event_key=f"runtime:{execution_id}:1",
            execution_id=str(execution_id),
            execution_sequence=1,
            event_type="capability.started",
            phase="mcp",
            state="running",
            safe_message="query_sku_identity_mapping_v1 running.",
            tool_name="query_sku_identity_mapping_v1",
            duration_ms=None,
            payload={"schema_version": "business-agent.activity-event.v1"},
            critical=False,
        ),
    )
    await repository.record_report_snapshot(
        target=target,
        snapshot={
            "status": "SUCCEEDED",
            "outputs": {
                "result": {
                    "result_artifact_ref": staged.payload_ref,
                    "result_artifact_hash": staged.payload_hash,
                    "item_count": 3,
                    "complete_count": 1,
                    "blocked_count": 1,
                    "failed_count": 1,
                    "summary_artifact_ref": None,
                    "risk_flags": ["SHORT_HISTORY"],
                }
            },
        },
        now=now,
    )
    first_page = await repository.get_report(
        tenant_id=tenant_id,
        report_run_id=created.report_run_id,
        item_offset=0,
        item_limit=2,
    )
    assert first_page is not None
    assert first_page["status"] == "PARTIAL"
    assert [item["ordinal"] for item in first_page["items"]] == [0, 1]
    assert first_page["next_item_offset"] == 2
    second_page = await repository.get_report(
        tenant_id=tenant_id,
        report_run_id=created.report_run_id,
        item_offset=2,
        item_limit=2,
    )
    assert second_page is not None
    assert [item["ordinal"] for item in second_page["items"]] == [2]
    assert second_page["next_item_offset"] is None
    activities = await repository.list_report_activities(
        tenant_id=tenant_id,
        report_run_id=created.report_run_id,
        after_sequence=0,
    )
    assert any(activity["event_type"] == "capability.started" for activity in activities)
    assert (
        await repository.get_report(
            tenant_id="other-tenant",
            report_run_id=created.report_run_id,
        )
        is None
    )
    await engine.dispose()
