from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest

from ebiz_deployment.supply_chain_bff.batch_result_contract import (
    BatchResultContractError,
    validated_batch_artifact,
    validated_batch_output,
)

CONTRACT_ROOT = Path(__file__).parents[1] / "contracts" / "supply-chain-level2"
FIXTURE = CONTRACT_ROOT / "fixtures" / "report-batch-results.valid.json"


def _artifact() -> dict[str, object]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_batch_output_and_restricted_artifact_are_validated_together() -> None:
    output = {
        "result_artifact_ref": "memory://payloads/v1/result",
        "result_artifact_hash": "a" * 64,
        "item_count": 3,
        "complete_count": 1,
        "blocked_count": 1,
        "failed_count": 1,
        "summary_artifact_ref": None,
        "risk_flags": ["SHORT_HISTORY"],
    }
    validated = validated_batch_output(
        {"outputs": {"result": output}},
        expected_item_count=3,
    )
    assert validated == output
    artifact = _artifact()
    assert (
        validated_batch_artifact(
            artifact,
            report_run_id=UUID("11111111-1111-4111-8111-111111111111"),
            batch_id=UUID("22222222-2222-4222-8222-222222222222"),
            item_offset=400,
            expected_item_count=3,
            expected_counts=(1, 1, 1),
        )
        == artifact
    )


def test_batch_output_accepts_an_inline_artifact_for_bff_materialization() -> None:
    artifact = _artifact()
    output = {
        "result_artifact": artifact,
        "item_count": 3,
        "complete_count": 1,
        "blocked_count": 1,
        "failed_count": 1,
        "summary_artifact_ref": None,
        "risk_flags": ["SHORT_HISTORY"],
    }
    assert (
        validated_batch_output(
            {"outputs": {"result": output}},
            expected_item_count=3,
        )
        == output
    )


def test_batch_output_rejects_mixed_inline_and_external_artifact_identity() -> None:
    output = {
        "result_artifact": _artifact(),
        "result_artifact_ref": "memory://payloads/v1/result",
        "result_artifact_hash": "a" * 64,
        "item_count": 3,
        "complete_count": 1,
        "blocked_count": 1,
        "failed_count": 1,
        "summary_artifact_ref": None,
        "risk_flags": [],
    }
    with pytest.raises(BatchResultContractError):
        validated_batch_output({"outputs": {"result": output}}, expected_item_count=3)


@pytest.mark.parametrize("field", ["result_artifact_ref", "result_artifact_hash", "item_count"])
def test_batch_output_fails_closed_when_identity_is_missing(field: str) -> None:
    output = {
        "result_artifact_ref": "memory://payloads/v1/result",
        "result_artifact_hash": "a" * 64,
        "item_count": 3,
        "complete_count": 1,
        "blocked_count": 1,
        "failed_count": 1,
        "summary_artifact_ref": None,
        "risk_flags": [],
    }
    del output[field]
    with pytest.raises(BatchResultContractError):
        validated_batch_output({"outputs": {"result": output}}, expected_item_count=3)


def test_artifact_rejects_cross_batch_identity_tampering_and_status_drift() -> None:
    artifact = _artifact()
    tampered = copy.deepcopy(artifact)
    tampered["batch_id"] = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(BatchResultContractError, match="identity"):
        validated_batch_artifact(
            tampered,
            report_run_id=UUID("11111111-1111-4111-8111-111111111111"),
            batch_id=UUID("22222222-2222-4222-8222-222222222222"),
            item_offset=400,
            expected_item_count=3,
            expected_counts=(1, 1, 1),
        )
    with pytest.raises(BatchResultContractError, match="status counts"):
        validated_batch_artifact(
            artifact,
            report_run_id=UUID("11111111-1111-4111-8111-111111111111"),
            batch_id=UUID("22222222-2222-4222-8222-222222222222"),
            item_offset=400,
            expected_item_count=3,
            expected_counts=(2, 1, 0),
        )


def test_artifact_schema_forbids_tenant_and_unknown_fields_recursively() -> None:
    artifact = _artifact()
    first = artifact["items"][0]  # type: ignore[index]
    first["tenant_id"] = "tenant-leak"  # type: ignore[index]
    with pytest.raises(BatchResultContractError, match="frozen schema"):
        validated_batch_artifact(
            artifact,
            report_run_id=UUID("11111111-1111-4111-8111-111111111111"),
            batch_id=UUID("22222222-2222-4222-8222-222222222222"),
            item_offset=400,
            expected_item_count=3,
            expected_counts=(1, 1, 1),
        )
