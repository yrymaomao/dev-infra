"""Required report facts survive the on-demand display projection or fail closed."""

import json
from pathlib import Path

import pytest

from ebiz_deployment.supply_chain_bff.batch_result_contract import BatchResultContractError
from ebiz_deployment.supply_chain_bff.conversation_result import (
    project_analysis,
    qualify_unowned_currency_symbols,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["remaining_inventory", "scenario_unit_price"])
@pytest.mark.parametrize("variant", ["missing", "null", "negative", "zero"])
async def test_on_demand_required_numeric_facts(field, variant):
    artifact = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts/supply-chain-level2/fixtures/report-batch-results.v2.valid.json"
        ).read_text(encoding="utf-8")
    )
    candidate = artifact["items"][0]["decision"]["candidates"][-1]
    if variant == "missing":
        del candidate[field]
    else:
        candidate[field] = {"null": None, "negative": -1, "zero": 0}[variant]
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
    if variant == "zero":
        projected = await project_analysis(None, "tenant-a", value)
        assert projected["items"][0]["decision"]["candidates"][-1][field] == 0
    else:
        with pytest.raises(BatchResultContractError):
            await project_analysis(None, "tenant-a", value)


def test_final_reply_removes_model_inferred_currency_symbols_when_result_has_no_currency():
    analyses = [{"items": [{"forecast": {"weeks": [{"revenue_mean": 14.76}]}}]}]

    assert qualify_unowned_currency_symbols(
        "Revenue is $14.76 and NPV is €3.20.", analyses
    ) == (
        "Revenue is 14.76 (currency unspecified) and NPV is 3.20 (currency unspecified)."
    )


def test_final_reply_keeps_symbols_when_result_explicitly_owns_currency():
    analyses = [{"currency": "USD", "value": 14.76}]

    assert qualify_unowned_currency_symbols("Revenue is $14.76.", analyses) == (
        "Revenue is $14.76."
    )
