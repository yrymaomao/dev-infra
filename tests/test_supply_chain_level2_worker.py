from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from ebiz_deployment.supply_chain_bff.level2_repository import ClaimedReportBatch
from ebiz_deployment.supply_chain_bff.level2_worker import (
    _batch_runtime_payload,
    _planner_result,
    _selection_result,
    _timestamp,
)


def test_bulk_runtime_payload_contains_refs_not_skus_and_is_stably_idempotent() -> None:
    claim = ClaimedReportBatch(
        event_id=UUID("00000000-0000-4000-8000-000000000001"),
        tenant_id="tenant-a",
        report_run_id=UUID("00000000-0000-4000-8000-000000000002"),
        batch_id=UUID("00000000-0000-4000-8000-000000000003"),
        batch_no=1,
        item_offset=0,
        item_count=200,
        selection_payload_ref="payload://selection/frozen",
        policy_snapshot_ref="payload://policy/v1",
        data_cutoff=datetime(2026, 9, 4, 12, tzinfo=UTC),
        lease_owner="worker-a",
    )
    payload = _batch_runtime_payload(claim)
    assert payload["idempotency_key"] == "supply-chain-report:00000000-0000-4000-8000-000000000003"
    assert payload["workflow"] == {
        "code": "inventory-supply-chain-batch-weekly",
        "version": 1,
    }
    assert "skus" not in str(payload)
    assert payload["inputs"] == {  # type: ignore[index]
        "report_run_id": "00000000-0000-4000-8000-000000000002",
        "batch_id": "00000000-0000-4000-8000-000000000003",
        "selection_snapshot_ref": "payload://selection/frozen",
        "item_offset": 0,
        "item_count": 200,
        "policy_snapshot_ref": "payload://policy/v1",
        "data_cutoff": "2026-09-04T12:00:00Z",
        "summary_enabled": True,
    }


def test_selection_result_is_fail_closed() -> None:
    valid = {
        "status": "SUCCEEDED",
        "outputs": {
            "result": {
                "selector": {
                    "quantity_metric": "AVAILABLE_QUANTITY",
                    "operator": "GT",
                    "threshold": 20,
                },
                "source_snapshot_id": "snapshot-1",
                "snapshot_time": "2026-09-04T12:00:00Z",
                "rows": [{"sku": "SKU-1"}],
                "warnings": [],
                "ambiguities": [],
            }
        },
    }
    assert _selection_result(valid)["rows"] == [{"sku": "SKU-1"}]
    assert _timestamp("2026-09-04T12:00:00Z") == datetime(2026, 9, 4, 12, tzinfo=UTC)
    with pytest.raises(ValueError):
        _selection_result({"status": "SUCCEEDED", "outputs": {"result": {}}})


def test_request_planner_result_is_strict_and_never_accepts_sql() -> None:
    selector, ambiguities = _planner_result(
        {
            "status": "SUCCEEDED",
            "outputs": {
                "result": {
                    "status": "READY",
                    "selector": {
                        "quantity_metric": "AVAILABLE_QUANTITY",
                        "operator": "GT",
                        "threshold": 20,
                    },
                    "ambiguities": [],
                }
            },
        }
    )
    assert selector["threshold"] == 20
    assert ambiguities == []
    with pytest.raises(ValueError):
        _planner_result(
            {
                "status": "SUCCEEDED",
                "outputs": {
                    "result": {
                        "status": "READY",
                        "selector": {
                            "quantity_metric": "AVAILABLE_QUANTITY",
                            "operator": "GT",
                            "threshold": 20,
                            "sql": "SELECT *",
                        },
                        "ambiguities": [],
                    }
                },
            }
        )
