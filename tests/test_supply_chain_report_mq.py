from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ebiz_deployment.supply_chain_bff.report_mq import ReportBatchMessage

FIXTURE = (
    Path(__file__).parents[1]
    / "contracts"
    / "supply-chain-level2"
    / "fixtures"
    / "report-batch-requested.valid.json"
)


def test_report_batch_message_matches_frozen_fixture() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    message = ReportBatchMessage.model_validate(payload)
    assert message.batch_no == 1
    assert message.item_count == 200
    assert not hasattr(message, "skus")


@pytest.mark.parametrize("forbidden", ["skus", "sql", "mcp_body", "credentials"])
def test_report_batch_message_rejects_sensitive_or_large_fields(forbidden: str) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload[forbidden] = ["SKU-1"]
    with pytest.raises(ValidationError):
        ReportBatchMessage.model_validate(payload)


def test_report_batch_message_enforces_batch_bounds() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["item_count"] = 201
    with pytest.raises(ValidationError):
        ReportBatchMessage.model_validate(payload)
