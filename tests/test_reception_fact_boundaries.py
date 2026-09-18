"""Configuration guardrails; real reply faithfulness still requires black-box review."""

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "required",
    [
        "unobserved weeks are not observed zero-sales weeks",
        "do not classify a new or short-history SKU as definitively slow-moving",
        "full-sale assumption",
        "cost_complete=false",
        "Never relabel net_cash_recovery_npv as gross revenue",
        "currency unspecified",
    ],
)
def test_reception_explains_forecast_and_scenario_limits(required):
    path = Path(__file__).parents[1] / "config/supply-chain-reception.v1.json"
    instructions = json.loads(path.read_text(encoding="utf-8"))["instructions"]
    assert required in instructions
