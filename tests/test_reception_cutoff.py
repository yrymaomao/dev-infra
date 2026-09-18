"""D01 guards: prompt configuration and public projection, not LLM acceptance."""

import json
from pathlib import Path

import pytest

from ebiz_deployment.supply_chain_bff.conversation_result import project_analysis

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "required",
    [
        "For follow-up explanations, explicitly restate the original data cutoff",
        "forecast.data_cutoff",
        "Preserve the full timestamp and timezone; this is an as-of result, not live inventory",
        "If the original cutoff is unavailable, say it is unavailable; never invent or replace it",
    ],
)
def test_reception_requires_original_cutoff_in_followups(required):
    # This guards deployment guidance only; it does not simulate a model reply.
    config = json.loads((ROOT / "config/supply-chain-reception.v1.json").read_text("utf-8"))
    assert required in config["instructions"]


@pytest.mark.parametrize(
    "required",
    [
        "When discussing an existing report, including correcting a mistaken "
        "replenishment premise, begin the reply with a standalone first line",
        "Data as of: <original timestamp with timezone>",
        "数据截止时间：<原始时间戳及其时区>",
        "Copy the complete original timestamp and timezone exactly; do not shorten it to a date",
        "Data as of: Unavailable",
        "数据截止时间：不可用",
        "Keep this first line even when the cutoff fact is unavailable; "
        "do not run a new analysis to fill it",
    ],
    ids=[
        "first-line-scope",
        "english",
        "chinese",
        "exact-timezone",
        "missing-en",
        "missing-zh",
        "no-rerun",
    ],
)
def test_reception_existing_report_first_line_format_static_guard(required):
    # Source-format guard only: cannot prove an actual model follows this format.
    config = json.loads((ROOT / "config/supply-chain-reception.v1.json").read_text("utf-8"))
    assert required in config["instructions"]


@pytest.mark.parametrize(
    "required",
    [
        "non-replayable does not mean the upstream service is permanently unavailable",
        "Never infer a technical root cause or permanent upstream outage from "
        "OPERATION_FAILED, category=permanent, or retryable=false alone",
        "If no explicit safe technical_cause is available in the authorized response, "
        "say the technical cause is unknown",
        "it does not authorize replay or automatic resubmission",
    ],
    ids=["disposition-not-duration", "no-top-level-inference", "unknown-cause", "no-replay"],
)
def test_reception_terminal_error_guidance_does_not_infer_upstream_cause(required):
    # Static guidance only: no simulated model or claim of real failure explanation.
    config = json.loads((ROOT / "config/supply-chain-reception.v1.json").read_text("utf-8"))
    assert required in config["instructions"]
    assert "Do not automatically resubmit a failed or uncertain operation" in config["instructions"]


def test_reception_cutoff_guidance_stays_within_existing_configuration_contract():
    config = json.loads((ROOT / "config/supply-chain-reception.v1.json").read_text("utf-8"))
    assert set(config) == {"version", "instructions", "tools", "argument_admissions"}
    assert config["argument_admissions"] == {
        "inventory_supply_chain_on_demand": {
            "mode": "exact_bare_token_array",
            "argument": "skus",
            "token_syntax": "ascii-identifier-v1",
            "max_items": 200,
        }
    }
    assert len(config["instructions"].encode("utf-8")) <= 16384
    assert set(config["tools"]) == {"inventory_supply_chain_on_demand"}
    assert (
        "Use prior conversation results for follow-up explanations without rerunning analysis"
        in config["instructions"]
    )


def test_reception_routes_all_letter_bare_sku_without_clarification():
    config = json.loads((ROOT / "config/supply-chain-reception.v1.json").read_text("utf-8"))
    assert "including an all-letter token such as abc" in config["instructions"]
    assert "Do not reject, reinterpret, or clarify a matching token" in config["instructions"]
    assert (
        "A single all-letter token such as abc is also a valid SKU input"
        in config["tools"]["inventory_supply_chain_on_demand"]
    )


def test_reception_batches_all_skus_into_exactly_one_tool_call_per_turn():
    config = json.loads((ROOT / "config/supply-chain-reception.v1.json").read_text("utf-8"))
    assert (
        "collect all distinct SKU tokens from the entire user message in first-occurrence order"
        in config["instructions"]
    )
    assert (
        "call inventory_supply_chain_on_demand exactly once with the complete deduplicated array"
        in config["instructions"]
    )
    assert "Never make one tool call per SKU" in config["instructions"]
    assert (
        "Use one invocation containing the complete deduplicated SKU array"
        in config["tools"]["inventory_supply_chain_on_demand"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff", ["2026-09-11T18:10:23Z", "2026-09-12T02:10:23+08:00"])
async def test_conversation_projection_preserves_exact_original_cutoff(cutoff):
    fixture = ROOT / "contracts/supply-chain-level2/fixtures/report-batch-results.v2.valid.json"
    artifact = json.loads(fixture.read_text("utf-8"))
    artifact["items"][0]["forecast"]["data_cutoff"] = cutoff
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
    projected = await project_analysis(None, "tenant-a", value)
    assert projected["items"][0]["forecast"]["data_cutoff"] == cutoff
