from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from ebiz_deployment.supply_chain_bff.level2_repository import ClaimedReportBatch
from ebiz_deployment.supply_chain_bff.level2_worker import (
    _batch_runtime_payload,
    _merge_selection_pages,
    _planner_result,
    _selection_result,
    _timestamp,
)


def _page(
    skus: list[str],
    *,
    snapshot: str = "snapshot-1",
    next_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "selector": {"quantity_metric": "AVAILABLE_QUANTITY", "operator": "GT", "threshold": 20},
        "source_snapshot_id": snapshot,
        "snapshot_time": "2026-09-04T12:00:00Z",
        "rows": [{"sku": sku, "available_quantity": 21} for sku in skus],
        "warnings": [],
        "ambiguities": [],
        "next_cursor": next_cursor,
    }


def test_selection_pages_freeze_snapshot_order_and_stop_at_10001() -> None:
    merged = _merge_selection_pages(
        [_page(["SKU-0001", "SKU-0002"], next_cursor="cursor-2"), _page(["SKU-0003"])]
    )
    assert [row["sku"] for row in merged["rows"]] == ["SKU-0001", "SKU-0002", "SKU-0003"]
    assert merged["source_snapshot_id"] == "snapshot-1"

    oversized_pages = []
    for page_no in range(51):
        start = page_no * 200
        end = min(start + 200, 10_001)
        oversized_pages.append(
            _page(
                [f"SKU-{index:05d}" for index in range(start, end)],
                next_cursor=(f"cursor-{page_no + 1}" if end < 10_001 else "stop-here"),
            )
        )
    oversized = _merge_selection_pages(oversized_pages)
    assert len(oversized["rows"]) == 10_001
    assert oversized["limit_reached"] is True


@pytest.mark.parametrize(
    "pages",
    [
        [_page(["SKU-1"], next_cursor="cursor"), _page(["SKU-2"], snapshot="changed")],
        [_page(["SKU-2", "SKU-1"])],
        [_page(["SKU-1"], next_cursor="cursor"), _page(["SKU-1"])],
        [_page(["SKU-1"], next_cursor="cursor"), _page(["SKU-2"], next_cursor="cursor")],
        [_page([f"SKU-{index:04d}" for index in range(201)])],
    ],
)
def test_selection_pages_fail_closed_on_drift_order_duplicate_or_cursor_stall(
    pages: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError):
        _merge_selection_pages(pages)


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
        "version": 6,
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
        "week_from": "2021-09-06",
        "week_to": "2026-08-24",
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
    v13 = {
        "status": "SUCCEEDED",
        "outputs": {
            "result": {
                "tenant_id": "tenant-a",
                "status": "COMPLETE",
                "scope": {"preview_id": "00000000-0000-4000-8000-000000000001"},
                "payload": {
                    "schema_version": "supply-chain.inventory-threshold.v1",
                    "source_snapshot_id": "snapshot-1",
                    "snapshot_time": "2026-09-04T12:00:00Z",
                    "items": [{"sku": "SKU-1", "available_quantity": 21}],
                    "next_cursor": None,
                },
                "evidence": [],
                "issues": [],
            }
        },
    }
    selector = {"quantity_metric": "AVAILABLE_QUANTITY", "operator": "GT", "threshold": 20}
    assert _selection_result(v13, selector=selector)["rows"][0]["sku"] == "SKU-1"
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
