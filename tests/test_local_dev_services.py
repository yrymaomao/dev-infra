"""Protocol tests for the loopback-only LOCAL_DEV_E2E providers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from ebiz_deployment import local_dev_assets as local_assets_module
from ebiz_deployment.local_dev_assets import build_skill_document, write_local_dev_assets
from ebiz_deployment.local_dev_services import (
    BROKER_AUTH_TOKEN,
    CREDENTIAL_REF,
    LOCAL_TENANT_ID,
    OPENAI_API_KEY,
    REQUEST_ACCESS_TOKEN,
    _local_inventory_as_of,
    create_mcp_app,
    create_provider_app,
)
from ebiz_deployment.record_attestation import attest_installed_distribution


@pytest.fixture(autouse=True)
def canonical_governed_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the generated-clock shape required by direct local MCP unit tests."""

    monkeypatch.setenv("SUPPLY_CHAIN_SNAPSHOT_TIME", "2026-08-30T00:00:00Z")


@pytest.fixture(scope="module")
def local_fixture_install(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ...]:
    root = Path(__file__).parents[1]
    output = tmp_path_factory.mktemp("local-fixture-wheel")
    completed = subprocess.run(
        [
            "uv",
            "build",
            str(root / "packages/local-evidence-fixture"),
            "--wheel",
            "--out-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheel = next(output.glob("ebiz_deployment_local_evidence_fixture-*.whl"))
    installed = tmp_path_factory.mktemp("local-fixture-install")
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    return (installed,)


def test_credential_broker_is_authenticated_scoped_and_short_lived() -> None:
    with TestClient(create_provider_app()) as client:
        unauthorized = client.post(
            "/v1/resolve",
            json={"credential_ref": CREDENTIAL_REF, "provider_id": "yeaher.erp"},
        )
        assert unauthorized.status_code == 401

        response = client.post(
            "/v1/resolve",
            headers={"Authorization": f"Bearer {BROKER_AUTH_TOKEN}"},
            json={"credential_ref": CREDENTIAL_REF, "provider_id": "yeaher.erp"},
        )
        assert response.status_code == 200
        assert response.json()["provider_id"] == "yeaher.erp"
        assert response.json()["access_token"] == REQUEST_ACCESS_TOKEN
        assert response.json()["tenant_id"] == "tenant-local-dev"
        assert len(response.json()["tenant_binding_digest"]) == 64
        assert response.json()["expires_at"].endswith("Z")

        rejected = client.post(
            "/v1/resolve",
            headers={"Authorization": f"Bearer {BROKER_AUTH_TOKEN}"},
            json={"credential_ref": CREDENTIAL_REF, "provider_id": "unknown.provider"},
        )
        assert rejected.status_code == 403


def test_provider_app_has_no_supply_chain_cockpit_route() -> None:
    with TestClient(create_provider_app()) as client:
        response = client.post(
            "/ai/read/v1/cockpit/product-performance/sku-windows",
            json={},
        )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("snapshot_time", "expected"),
    [
        ("2026-09-01T01:02:03Z", "2026-09-01T01:02:03Z"),
        ("2024-02-29T23:59:59Z", "2024-02-29T23:59:59Z"),
        ("2027-01-01T00:00:00Z", "2027-01-01T00:00:00Z"),
    ],
)
def test_local_inventory_as_of_accepts_only_canonical_governed_snapshots(
    monkeypatch: pytest.MonkeyPatch, snapshot_time: str, expected: str
) -> None:
    monkeypatch.setenv("SUPPLY_CHAIN_SNAPSHOT_TIME", snapshot_time)

    assert _local_inventory_as_of() == expected


def test_local_inventory_as_of_requires_the_governed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPPLY_CHAIN_SNAPSHOT_TIME", raising=False)

    with pytest.raises(ValueError, match="SUPPLY_CHAIN_SNAPSHOT_TIME is required"):
        _local_inventory_as_of()


@pytest.mark.parametrize(
    "snapshot_time",
    [
        "2026-9-1T1:2:3Z",
        "2026-09-01T01:02:03+00:00",
        "2026-09-01T01:02:03.000Z",
    ],
)
def test_local_inventory_as_of_rejects_noncanonical_governed_snapshots(
    monkeypatch: pytest.MonkeyPatch, snapshot_time: str
) -> None:
    monkeypatch.setenv("SUPPLY_CHAIN_SNAPSHOT_TIME", snapshot_time)

    with pytest.raises(ValueError, match="canonical RFC3339 UTC seconds"):
        _local_inventory_as_of()


def test_classification_connector_allowlist_matches_v4_workflow_inputs() -> None:
    operation = local_assets_module._CONNECTOR_FIELD_ALLOWLISTS[
        "supply-chain-planning.classification-engine@2.0.0"
    ]["supply_chain.classify_inventory"]

    assert set(operation) == {
        "cohort",
        "cohort_total_eligible",
        "forecast",
        "growth_ratio",
        "growth_unavailable_reason",
        "seasonality_profile",
        "snapshot_time",
    }


def test_openai_responses_endpoint_returns_schema_valid_json_text() -> None:
    schema = {
        "$schema": "https://schemas.ebizhub.com/meta/runtime-contract/v1.2",
        "type": "object",
        "additionalProperties": False,
        "required": ["explanation", "risk_flags"],
        "properties": {
            "explanation": {"type": "string", "minLength": 1, "maxLength": 4000},
            "risk_flags": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }
    request = {
        "model": "local-dev-seasonality-model",
        "input": [{"role": "user", "content": "analyze"}],
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "structured_output",
                "schema": schema,
                "strict": True,
            }
        },
    }
    with TestClient(create_provider_app()) as client:
        unauthorized = client.post("/v1/responses", json=request)
        assert unauthorized.status_code == 401

        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=request,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "local-dev-seasonality-model"
    output = json.loads(body["output"][0]["content"][0]["text"])
    Draft202012Validator(schema).validate(output)
    assert output == {
        "explanation": "Deterministic local development analysis.",
        "risk_flags": [],
    }


def test_openai_responses_endpoint_selects_only_schema_authorized_business_issue() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["issue_code", "message", "blocking", "metadata"],
        "properties": {
            "issue_code": {"const": "SKU_NOT_FOUND"},
            "message": {"enum": ["This SKU could not be found."]},
            "blocking": {"const": True},
            "metadata": {"type": "object", "additionalProperties": False},
        },
    }
    request = {
        "model": "local-dev-business-outcome-model",
        "input": [{"role": "user", "content": "explain"}],
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "structured_output",
                "schema": schema,
                "strict": True,
            }
        },
    }

    with TestClient(create_provider_app()) as client:
        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=request,
        )

    assert response.status_code == 200
    output = json.loads(response.json()["output"][0]["content"][0]["text"])
    assert output == {
        "issue_code": "SKU_NOT_FOUND",
        "message": "This SKU could not be found.",
        "blocking": True,
        "metadata": {},
    }


def test_openai_responses_endpoint_rejects_non_exact_or_unsatisfied_schema() -> None:
    valid = {
        "model": "local-dev-seasonality-model",
        "input": [{"role": "user", "content": "analyze"}],
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "structured_output",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["explanation", "risk_flags"],
                    "properties": {
                        "explanation": {"type": "string"},
                        "risk_flags": {"type": "array"},
                    },
                },
            }
        },
    }
    invalid_requests = []
    for field, value in (
        ("type", "text"),
        ("name", "other"),
        ("strict", False),
    ):
        request = deepcopy(valid)
        request["text"]["format"][field] = value
        invalid_requests.append(request)
    schema_rejects_output = deepcopy(valid)
    schema_rejects_output["text"]["format"]["schema"]["required"] = ["missing"]
    invalid_requests.append(schema_rejects_output)
    extra_format_field = deepcopy(valid)
    extra_format_field["text"]["format"]["extra"] = True
    invalid_requests.append(extra_format_field)

    with TestClient(create_provider_app()) as client:
        responses = [
            client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json=request,
            )
            for request in invalid_requests
        ]

    assert [response.status_code for response in responses] == [422] * len(responses)


def test_mcp_requires_broker_token_and_exposes_only_supply_chain_v5_read_tools() -> None:
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "local-test", "version": "1"},
        },
    }
    headers = {
        "X-Mcp-Key": REQUEST_ACCESS_TOKEN,
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_mcp_app(), base_url="http://127.0.0.1:18081") as client:
        unauthorized = client.post("/mcp", json=init, headers={"Accept": headers["Accept"]})
        assert unauthorized.status_code == 401
        initialized = client.post("/mcp", json=init, headers=headers)
        assert initialized.status_code == 200
        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
        identity = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "query_sku_upc_mapping",
                    "arguments": {
                        "pageIndex": 1,
                        "pageSize": 2,
                        "skuCode": ["SKU-LOCAL-1"],
                        "searchType": "exactSearch",
                    },
                },
            },
            headers=headers,
        )
        inventory = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "query_inventory_summary_v2",
                    "arguments": {
                        "marketScope": "NA_COMPANY",
                        "skuCode": "SKU-LOCAL-1",
                    },
                },
            },
            headers=headers,
        )
        windows = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "query_sku_sales_profit_windows_v1",
                    "arguments": {
                        "marketScope": "NA_COMPANY",
                        "snapshotDate": "20260830",
                        "skuCode": "SKU-LOCAL-1",
                    },
                },
            },
            headers=headers,
        )
        cohort = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "query_sku_boston_cohort_v1",
                    "arguments": {
                        "marketScope": "NA_COMPANY",
                        "snapshotDate": "20260830",
                        "targetSkuCode": "SKU-LOCAL-1",
                        "pageSize": 1000,
                    },
                },
            },
            headers=headers,
        )

    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {
        "query_sku_upc_mapping",
        "query_inventory_summary_v2",
        "query_sku_sales_profit_windows_v1",
        "query_sku_boston_cohort_v1",
    }
    for tool in listed.json()["result"]["tools"]:
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["outputSchema"]["additionalProperties"] is False
    cohort_tool = next(
        tool
        for tool in listed.json()["result"]["tools"]
        if tool["name"] == "query_sku_boston_cohort_v1"
    )
    cohort_input = cohort_tool["inputSchema"]
    assert set(cohort_input["required"]) == {
        "targetSkuCode",
        "marketScope",
        "snapshotDate",
        "pageSize",
    }
    assert cohort_input["properties"]["pageSize"]["minimum"] == 1
    assert cohort_input["properties"]["pageSize"]["maximum"] == 1000
    assert cohort_input["properties"]["snapshotDate"]["pattern"] == r"^\d{8}$"
    cohort_output = cohort_tool["outputSchema"]
    assert set(cohort_output["properties"]) == {"success", "code", "message", "result"}
    member_schema = cohort_output["properties"]["result"]["properties"]["members"]["items"]
    assert member_schema["additionalProperties"] is False
    assert identity.status_code == 200
    identity_document = identity.json()["result"]["structuredContent"]
    assert set(identity_document) == {"success", "code", "message", "result"}
    assert identity_document["success"] is True
    assert identity_document["code"] == 200
    assert identity_document["message"] is None
    identity_page = identity_document["result"]
    assert identity_page["total"] == 1
    assert identity_page["current"] == 1
    assert identity_page["size"] == 2
    assert identity_page["pages"] == 1
    identity_record = identity_page["records"][0]
    assert set(identity_record) == {
        "skuName",
        "barCode",
        "skuCode",
        "manager",
        "newStatus",
        "currency",
        "cost",
        "spuCode",
        "spuName",
        "brand",
        "category",
        "note",
        "productType",
        "isContainUpgrade",
        "isAccessory",
        "isUpgradeAccessory",
        "categoryId",
        "amazonCategoryName",
        "path",
        "fullPath",
        "isToBeDeprecated",
        "isUpgradable",
        "subSkuResultDtos",
        "imageUrls",
        "upid",
        "createTime",
        "createBy",
    }
    assert identity_record["skuCode"] == "SKU-LOCAL-1"
    assert identity_record["barCode"] == "UPC-LOCAL-1"
    assert identity_record["cost"] == 12.5
    assert identity_record["path"] == [
        {
            "amazonCategoryId": "LOCAL-CATEGORY",
            "amazonCategoryName": "Local category",
            "categoryId": "LOCAL-CATEGORY",
            "fullPath": "Local category",
        }
    ]
    assert identity_record["subSkuResultDtos"] == []
    assert identity_record["imageUrls"] == []
    assert inventory.status_code == 200
    inventory_document = inventory.json()["result"]["structuredContent"]
    assert set(inventory_document) == {"code", "message", "result"}
    assert inventory_document["code"] == 200
    assert inventory_document["message"] == "success"
    assert inventory_document["result"] == {
        "status": "FOUND",
        "sku": "SKU-LOCAL-1",
        "availableQuantity": 18,
        "holdQuantity": 0,
        "transferInTransitQuantity": 0,
        "purchaseInTransitQuantity": 12,
        "agedInventoryQuantity": 0,
        "daysSinceLastSale": 1,
        "sourceSnapshotId": "local-inventory-snapshot-1",
        "asOf": "2026-08-30T00:00:00Z",
    }
    assert windows.status_code == 200
    windows_document = windows.json()["result"]["structuredContent"]
    assert set(windows_document) == {"success", "code", "message", "result"}
    assert windows_document["success"] is True
    assert windows_document["code"] == 200
    assert windows_document["message"] is None
    assert windows_document["result"]["cid"] == LOCAL_TENANT_ID
    assert windows_document["result"]["snapshotDate"] == "20260830"
    assert windows_document["result"]["items"][0]["skuCode"] == "SKU-LOCAL-1"
    assert [item["windowDays"] for item in windows_document["result"]["items"][0]["windows"]] == [
        7,
        14,
        30,
        60,
        90,
        180,
        365,
    ]
    assert cohort.status_code == 200
    call_result = cohort.json()["result"]
    assert call_result["isError"] is False
    cohort_document = call_result["structuredContent"]
    assert set(cohort_document) == {"success", "code", "message", "result"}
    cohort_result = cohort_document["result"]
    assert set(cohort_result) == {
        "cid",
        "marketScope",
        "snapshotDate",
        "statisticsVersion",
        "currency",
        "status",
        "filterDefinition",
        "totalEligible",
        "snapshotId",
        "nextCursor",
        "members",
        "sourceMaxBizDate",
        "sourceWatermark",
        "incompleteReason",
    }
    assert cohort_result["snapshotDate"] == "20260830"
    assert cohort_result["sourceWatermark"] == "local-cohort-watermark-20260829"
    assert cohort_result["nextCursor"] is None
    assert all(
        member["skuCode"].startswith("SKU-LOCAL-PEER-") for member in cohort_result["members"]
    )


def test_mcp_fixture_uses_the_generated_snapshot_for_freshness_across_a_date_rollover(
    tmp_path: Path, local_fixture_install: tuple[Path, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later deterministic asset snapshot must keep every ERP evidence watermark eligible."""

    monkeypatch.setattr(
        local_assets_module,
        "_installed_digests",
        lambda: {
            "COMMERCE_SALES_CATALOG_RECORD_DIGEST": "1" * 64,
            "ERP_RECORD_DIGEST": "2" * 64,
            "INVENTORY_CATALOG_RECORD_DIGEST": "3" * 64,
            "MCP_RECORD_DIGEST": "4" * 64,
            "OPENAI_RECORD_DIGEST": "5" * 64,
            "SUPPLY_CHAIN_AGENT_RECORD_DIGEST": "6" * 64,
            "SUPPLY_CHAIN_PLANNING_RECORD_DIGEST": "7" * 64,
            "SUPPLY_CHAIN_WORKFLOW_DIGEST": "8" * 64,
        },
    )
    assets = write_local_dev_assets(
        tmp_path,
        fixture_search_paths=local_fixture_install,
        snapshot_time=datetime(2027, 1, 2, 0, 5, 6, tzinfo=UTC),
    )
    environment = json.loads(assets.environment.read_text(encoding="utf-8"))
    monkeypatch.setenv("SUPPLY_CHAIN_SNAPSHOT_TIME", environment["SUPPLY_CHAIN_SNAPSHOT_TIME"])
    headers = {
        "X-Mcp-Key": REQUEST_ACCESS_TOKEN,
        "Accept": "application/json, text/event-stream",
    }
    initialized = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "local-test", "version": "1"},
        },
    }
    with TestClient(create_mcp_app(), base_url="http://127.0.0.1:18081") as client:
        assert client.post("/mcp", json=initialized, headers=headers).status_code == 200
        inventory = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "query_inventory_summary_v2",
                    "arguments": {"marketScope": "NA_COMPANY", "skuCode": "SKU-LOCAL-1"},
                },
            },
            headers=headers,
        )
        windows = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "query_sku_sales_profit_windows_v1",
                    "arguments": {
                        "marketScope": "NA_COMPANY",
                        "snapshotDate": "20270102",
                        "skuCode": "SKU-LOCAL-1",
                    },
                },
            },
            headers=headers,
        )
        cohort = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "query_sku_boston_cohort_v1",
                    "arguments": {
                        "marketScope": "NA_COMPANY",
                        "snapshotDate": "20270102",
                        "targetSkuCode": "SKU-LOCAL-1",
                        "pageSize": 1000,
                    },
                },
            },
            headers=headers,
        )

    inventory_result = inventory.json()["result"]["structuredContent"]["result"]
    windows_result = windows.json()["result"]["structuredContent"]["result"]
    cohort_result = cohort.json()["result"]["structuredContent"]["result"]
    assert inventory_result["asOf"] == "2027-01-02T00:05:06Z"
    assert windows_result["items"][0]["sourceMaxBizDate"] == "20270101"
    assert windows_result["items"][0]["sourceWatermark"] == "local-sales-watermark-20270101"
    assert cohort_result["sourceMaxBizDate"] == "20270101"
    assert cohort_result["sourceWatermark"] == "local-cohort-watermark-20270101"


def test_local_only_sales_unavailable_fixture_preserves_null_source_provenance() -> None:
    """Keep the real Java-shaped unavailable envelope available to the local E2E seam only."""

    headers = {
        "X-Mcp-Key": REQUEST_ACCESS_TOKEN,
        "Accept": "application/json, text/event-stream",
    }
    initialized = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "local-test", "version": "1"},
        },
    }
    with TestClient(create_mcp_app(), base_url="http://127.0.0.1:18081") as client:
        assert client.post("/mcp", json=initialized, headers=headers).status_code == 200
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "query_sku_sales_profit_windows_v1",
                    "arguments": {
                        "marketScope": "NA_COMPANY",
                        "snapshotDate": "20260830",
                        "skuCode": "SKU-LOCAL-NO-WINDOW-DATA",
                    },
                },
            },
            headers=headers,
        )

    assert response.status_code == 200
    item = response.json()["result"]["structuredContent"]["result"]["items"][0]
    assert item == {
        "skuCode": "SKU-LOCAL-NO-WINDOW-DATA",
        "status": "NO_WINDOW_DATA",
        "incompleteReason": None,
        "windows": [],
        "activeDays": None,
        "growthRatio": None,
        "growthUnavailableReason": None,
        "currency": "USD",
        "sourceMaxBizDate": None,
        "sourceWatermark": None,
        "calculationVersion": "sku-sales-profit-v1",
    }


@pytest.mark.parametrize(
    "name, arguments",
    [
        (
            "query_sku_sales_profit_windows_v1",
            {"marketScope": "NA_COMPANY", "snapshotDate": "20260230", "skuCode": "SKU-LOCAL-1"},
        ),
        (
            "query_inventory_summary_v2",
            {"marketScope": "NA_COMPANY", "skuCode": "   "},
        ),
        (
            "query_sku_boston_cohort_v1",
            {
                "marketScope": "NA_COMPANY",
                "snapshotDate": "20260830",
                "targetSkuCode": "   ",
                "pageSize": 1,
            },
        ),
        (
            "query_sku_upc_mapping",
            {"pageIndex": 1, "pageSize": 2, "skuCode": ["   "], "searchType": "exactSearch"},
        ),
    ],
)
def test_mcp_rejects_invalid_java_request_fields_as_tool_errors(
    name: str, arguments: dict[str, object]
) -> None:
    headers = {
        "X-Mcp-Key": REQUEST_ACCESS_TOKEN,
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_mcp_app(), base_url="http://127.0.0.1:18081") as client:
        initialized = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "local-test", "version": "1"},
                },
            },
            headers=headers,
        )
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=headers,
        )

    assert initialized.status_code == 200
    assert response.status_code == 200
    body = response.json()
    assert "structuredContent" not in body.get("result", {})
    assert "error" in body or body["result"]["isError"] is True


def test_local_assets_are_closed_valid_and_explicitly_non_production(
    tmp_path: Path, local_fixture_install: tuple[Path, ...]
) -> None:
    skill = build_skill_document(
        snapshot_time="2026-08-24T00:00:00Z",
        tenant_id="tenant-local-dev",
        market_scope="NA_COMPANY",
        sku="SKU-LOCAL-1",
    )
    assert skill["policy_version"] == "local-dev-v5"
    assert len(skill["seasonality_profile"]["monthly_indices"]) == 12
    assert sum(item["index"] for item in skill["seasonality_profile"]["monthly_indices"]) == 12

    assets = write_local_dev_assets(
        tmp_path,
        fixture_search_paths=local_fixture_install,
        snapshot_time=datetime(2026, 8, 31, 6, 0, tzinfo=UTC),
    )
    assert assets.deployment_config.is_file()
    assert assets.plugin_policy.is_file()
    assert assets.base_ai_attestation.is_file()
    assert assets.fixture_plugin_policy.is_file()
    assert assets.fixture_contract_root.joinpath("capabilities.yaml").is_file()
    assert assets.model_schemas.is_file()
    assert assets.tls_ca.is_file()
    assert assets.tls_cert.is_file()
    assert assets.tls_key.is_file()
    assert assets.skill_root.joinpath("SKU-LOCAL-1.json").is_file()
    agent = metadata.distribution("ebiz-agent-inventory-supply-chain")
    agent_schema_path = Path(
        agent.locate_file("inventory_supply_chain_agent/schemas/seasonality-analysis.schema.yaml")
    )
    expected_model_schema = yaml.safe_load(agent_schema_path.read_text(encoding="utf-8"))
    model_schemas = json.loads(assets.model_schemas.read_text(encoding="utf-8"))
    assert model_schemas == {"schemas/seasonality-analysis.schema.yaml": expected_model_schema}
    policy = json.loads(assets.plugin_policy.read_text(encoding="utf-8"))
    assert {item["plugin_id"] for item in policy["plugins"]} == {
        "deployment.fixture.governed-artifact",
        "supply-chain-planning",
    }
    assert all("${" not in item["package_digest"] for item in policy["plugins"])
    assert all(len(set(item["package_digest"])) > 1 for item in policy["plugins"])
    environment = json.loads(assets.environment.read_text(encoding="utf-8"))
    assert environment["APP_ENV"] == "local_dev"
    assert environment["LOCAL_DEV_E2E"] == "true"
    assert environment["CREDENTIAL_BROKER_URL"] == "http://127.0.0.1:18082/v1/resolve"
    assert environment["MCP_ENDPOINT"] == "http://127.0.0.1:18081/mcp"
    assert environment["OPENAI_ENDPOINT"] == "https://localhost:18080/v1"
    assert environment["SUPPLY_CHAIN_MARKET_SCOPE"] == "NA_COMPANY"
    assert environment["BASE_AI_PROVIDER_ATTESTATION_PATH"] == str(assets.base_ai_attestation)
    assert environment["LOCAL_FIXTURE_PLUGIN_POLICY_PATH"] == str(assets.fixture_plugin_policy)
    assert environment["LOCAL_FIXTURE_CONTRACT_ROOT"] == str(assets.fixture_contract_root)
    assert environment["SUPPLY_CHAIN_SKILL_INPUT_REF"] == ""
    assert (
        environment["SUPPLY_CHAIN_SKILL_FILE_SHA256"]
        == hashlib.sha256(Path(environment["SUPPLY_CHAIN_SKILL_FILE"]).read_bytes()).hexdigest()
    )
    assert environment["SUPPLY_CHAIN_RUN_ID"] == "supply-chain-v5-local-1"
    assert environment["SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT"] == "5"
    assert environment["SUPPLY_CHAIN_EXPECTED_RESULT_STATUS"] == "COMPLETE"
    assert environment["SUPPLY_CHAIN_SNAPSHOT_TIME"] == "2026-08-31T06:00:00Z"
    targets = set(environment["APP_CONNECTOR_TARGETS"].split(","))
    assert targets == {
        "yeaher.erp@0.1.1",
        "supply-chain-planning.fulfillment-resolver@2.0.0",
        "supply-chain-planning.forecast-engine@2.0.0",
        "supply-chain-planning.classification-engine@2.0.0",
        "supply-chain-planning.action-router@2.0.0",
        "supply-chain-planning.replenishment-engine@2.0.0",
        "supply-chain-planning.clearance-engine@2.0.0",
    }
    field_policies = json.loads(environment["APP_CONNECTOR_FIELD_ALLOWLISTS"])
    assert set(field_policies) == targets
    assert "mcp.streamable_http@0.1.0" not in field_policies
    assert "deployment.fixture.governed-artifact@1.0.1" not in field_policies
    assert "COCKPIT_ENDPOINT" not in environment


def test_local_asset_generation_is_idempotent_without_importing_agent(
    tmp_path: Path, local_fixture_install: tuple[Path, ...]
) -> None:
    snapshot = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
    first = write_local_dev_assets(
        tmp_path, fixture_search_paths=local_fixture_install, snapshot_time=snapshot
    )
    second = write_local_dev_assets(
        tmp_path, fixture_search_paths=local_fixture_install, snapshot_time=snapshot
    )

    assert first.environment == second.environment
    assert second.environment.is_file()


def test_local_asset_default_snapshot_is_generated_at_run_time(
    tmp_path: Path, local_fixture_install: tuple[Path, ...]
) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    assets = write_local_dev_assets(tmp_path, fixture_search_paths=local_fixture_install)
    after = datetime.now(UTC).replace(microsecond=0)
    environment = json.loads(assets.environment.read_text(encoding="utf-8"))
    observed = datetime.fromisoformat(
        environment["SUPPLY_CHAIN_SNAPSHOT_TIME"].replace("Z", "+00:00")
    )

    assert before <= observed <= after


def test_local_asset_generation_rejects_naive_snapshot_time(
    tmp_path: Path, local_fixture_install: tuple[Path, ...]
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        write_local_dev_assets(
            tmp_path,
            fixture_search_paths=local_fixture_install,
            snapshot_time=datetime(2026, 8, 31, 6, 0),
        )


def test_local_fixture_and_planning_plugins_load_only_from_combined_local_policy(
    tmp_path: Path, local_fixture_install: tuple[Path, ...]
) -> None:
    assets = write_local_dev_assets(tmp_path, fixture_search_paths=local_fixture_install)
    script = """
import asyncio
from pathlib import Path
from agent_runtime.plugins.manifest import PluginHostPolicy
from agent_runtime.plugins.registry import PluginRegistry

policy = PluginHostPolicy.model_validate_json(Path(__import__('sys').argv[1]).read_text())
registry = PluginRegistry(policy=policy, supported_api_version='ebizhub.runtime/v1')
snapshot = asyncio.run(registry.load_startup())
assert len(snapshot.plugins) == 2
assert len(snapshot.providers) == 7
print('local-fixture-ready')
"""
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in local_fixture_install)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(assets.plugin_policy)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "local-fixture-ready"


def test_local_assets_pin_the_final_fixture_install_and_fail_closed_after_digest_change(
    tmp_path: Path, local_fixture_install: tuple[Path, ...]
) -> None:
    assets = write_local_dev_assets(tmp_path, fixture_search_paths=local_fixture_install)
    policy = json.loads(assets.plugin_policy.read_text(encoding="utf-8"))
    fixture = next(
        item
        for item in policy["plugins"]
        if item["plugin_id"] == "deployment.fixture.governed-artifact"
    )
    attestation = attest_installed_distribution(
        distribution_name="ebiz-deployment-local-evidence-fixture",
        distribution_version="1.0.1",
        entry_point_group="ebiz_agents.providers",
        entry_point_name="deployment.fixture.governed-artifact",
        entry_point_value="ebiz_deployment_local_fixture.plugin:factory",
        search_paths=local_fixture_install,
    )
    assert fixture["package_digest"] == attestation.canonical_digest

    fixture["package_digest"] = "0" * 64
    stale_policy = tmp_path / "stale-runtime-plugin-policy.json"
    stale_policy.write_text(json.dumps(policy), encoding="utf-8")
    script = """
import asyncio
from pathlib import Path
from agent_runtime.plugins.manifest import PluginHostPolicy
from agent_runtime.plugins.registry import PluginRegistry
from agent_runtime.plugins.contracts import PluginHostError

policy = PluginHostPolicy.model_validate_json(Path(__import__('sys').argv[1]).read_text())
registry = PluginRegistry(policy=policy, supported_api_version='ebizhub.runtime/v1')
try:
    asyncio.run(registry.load_startup())
except PluginHostError as exc:
    print(exc.error_code, file=__import__('sys').stderr)
    raise
"""
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in local_fixture_install)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(stale_policy)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
    )

    assert completed.returncode != 0
    assert "PLUGIN_PACKAGE_DIGEST_MISMATCH" in completed.stderr
