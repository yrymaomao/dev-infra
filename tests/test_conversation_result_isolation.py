"""Scoped projection checks; not a substitute for live conversation authorization."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ebiz_deployment.supply_chain_bff.conversation_result import project_analysis


def result_for(prefix: str) -> dict:
    artifact = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts/supply-chain-level2/fixtures/report-batch-results.v2.valid.json"
        ).read_text(encoding="utf-8")
    )
    for index, item in enumerate(artifact["items"]):
        item["sku"] = f"{prefix}-{index}"
        if item["forecast"]:
            item["forecast"]["sku"] = item["sku"]
            item["forecast"]["feature_snapshot_ref"] = f"snapshot-{prefix}-{index}#{item['sku']}"
    return {
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


@pytest.mark.asyncio
async def test_separate_result_projections_preserve_each_sku_status_and_reason():
    first = result_for("FX-RESULT-A")
    second = result_for("FX-RESULT-B")
    first_projection = await project_analysis(None, "tenant-a", first)
    second_projection = await project_analysis(None, "tenant-a", second)
    for original, projected in [(first, first_projection), (second, second_projection)]:
        source = original["payload"]["result_artifact"]["items"]
        source_artifact = original["payload"]["result_artifact"]
        assert projected["schema_version"] == "supply-chain.report-batch-results.v2"
        assert projected["report_run_id"] == source_artifact["report_run_id"]
        assert projected["batch_id"] == source_artifact["batch_id"]
        assert projected["item_offset"] == source_artifact["item_offset"]
        assert [item["sku"] for item in projected["items"]] == [item["sku"] for item in source]
        assert [item["result_status"] for item in projected["items"]] == [
            "COMPLETE",
            "BLOCKED",
            "FAILED",
        ]
        for actual, expected in zip(projected["items"], source, strict=True):
            assert actual["ordinal"] == expected["ordinal"]
            assert actual["business_issues"] == expected["business_issues"]
            assert actual["evidence_refs"] == expected["evidence_refs"]
            assert actual["lane_breakdown"] == expected["lane_breakdown"]
            assert actual["runtime_error"] == expected["runtime_error"]
        projected_forecast = projected["items"][0]["forecast"]
        source_forecast = source[0]["forecast"]
        for field in (
            "schema_version",
            "sku",
            "model_version",
            "data_cutoff",
            "feature_snapshot_ref",
            "risk_flags",
            "weeks",
        ):
            assert projected_forecast[field] == source_forecast[field]
    assert "FX-RESULT-B" not in json.dumps(first_projection)
    assert "FX-RESULT-A" not in json.dumps(second_projection)


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_tenant", ["tenant-b", None])
async def test_tenant_mismatch_rejected_before_loading_any_result(claimed_tenant):
    repository = SimpleNamespace(_load=AsyncMock())
    value = result_for("FX-UNAUTHORIZED")
    value["tenant_id"] = claimed_tenant
    with pytest.raises(ValueError, match="Conversation result tenant mismatch"):
        await project_analysis(repository, "tenant-a", value)
    repository._load.assert_not_awaited()
