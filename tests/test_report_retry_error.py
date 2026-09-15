"""A recovered dispatch failure must not masquerade as a final Runtime failure."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from ebiz_deployment.supply_chain_bff.level2_repository import (
    ActiveReportExecution,
    Level2Repository,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED"])
async def test_terminal_snapshot_replaces_obsolete_dispatch_error(status):
    artifact = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts"
            / "supply-chain-level2"
            / "fixtures"
            / "report-batch-results.v2.valid.json"
        ).read_text()
    )
    target = ActiveReportExecution(
        tenant_id="tenant-a",
        report_run_id=UUID(artifact["report_run_id"]),
        batch_id=UUID(artifact["batch_id"]),
        execution_id=uuid4(),
        sequence=0,
        item_offset=400,
        item_count=3,
    )
    batch = SimpleNamespace(
        id=target.batch_id,
        report_run_id=target.report_run_id,
        tenant_id=target.tenant_id,
        batch_no=1,
        status="RUNNING",
        safe_error={"error_code": "OLD_DISPATCH_FAILURE"},
    )
    session = MagicMock()
    session.begin.return_value = AsyncMock()
    session.scalar = AsyncMock(
        side_effect=["supply-chain.report.v2", batch] if status == "SUCCEEDED" else [batch]
    )
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    repository = Level2Repository(factory, payload_store=object())
    repository._stage = AsyncMock(
        return_value=SimpleNamespace(payload_ref="test-ref", payload_hash="a" * 64)
    )
    repository._commit_staged = AsyncMock()
    repository._refresh_report = AsyncMock()
    snapshot = {
        "status": status,
        "error": {"error_code": "CURRENT_RUNTIME_FAILURE", "safe_message": "Execution failed."},
        "outputs": {
            "result": {
                "result_artifact": artifact,
                "item_count": 3,
                "complete_count": 1,
                "blocked_count": 1,
                "failed_count": 1,
                "summary_artifact_ref": None,
                "risk_flags": [],
            }
        },
    }
    await repository.record_report_snapshot(target=target, snapshot=snapshot, now=datetime.now(UTC))
    if status == "SUCCEEDED":
        assert batch.safe_error is None
        assert batch.status == "PARTIAL"
        assert (batch.complete_count, batch.blocked_count, batch.failed_count) == (1, 1, 1)
        assert repository._stage.await_args.args[1]["items"] == artifact["items"]
    else:
        assert batch.status == "FAILED"
        assert batch.failed_count == 3
        assert batch.safe_error["error_code"] == "CURRENT_RUNTIME_FAILURE"
