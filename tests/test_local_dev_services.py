"""Protocol tests for the loopback-only LOCAL_DEV_E2E providers."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from inventory_supply_chain.contracts import validate_skill_input

from ebiz_deployment.local_dev_assets import build_skill_document, write_local_dev_assets
from ebiz_deployment.local_dev_services import (
    BROKER_AUTH_TOKEN,
    CREDENTIAL_REF,
    OPENAI_API_KEY,
    REQUEST_ACCESS_TOKEN,
    create_mcp_app,
    create_provider_app,
)


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
        assert response.json()["expires_at"].endswith("Z")

        rejected = client.post(
            "/v1/resolve",
            headers={"Authorization": f"Bearer {BROKER_AUTH_TOKEN}"},
            json={"credential_ref": CREDENTIAL_REF, "provider_id": "unknown.provider"},
        )
        assert rejected.status_code == 403


def test_cockpit_returns_deterministic_contract_for_requested_scope() -> None:
    request = {
        "marketplace": "US",
        "sku": "SKU-LOCAL-1",
        "snapshotTime": "2026-08-24T00:00:00Z",
        "pageSize": 200,
    }
    with TestClient(create_provider_app()) as client:
        unauthorized = client.post(
            "/ai/read/v1/cockpit/product-performance/sku-windows", json=request
        )
        assert unauthorized.status_code == 401

        response = client.post(
            "/ai/read/v1/cockpit/product-performance/sku-windows",
            headers={"Cookie": f"_token_={REQUEST_ACCESS_TOKEN}"},
            json=request,
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["tenantId"] == "tenant-local-dev"
    assert result["marketplace"] == "US"
    assert result["sku"] == "SKU-LOCAL-1"
    assert result["observedAt"] == "2026-08-24T00:00:00Z"
    assert len(result["seasonality"]["points"]) == 730
    assert len(result["bostonCohort"]) == 5
    assert result["returnedSize"] == result["totalSize"] == 5


def test_openai_responses_endpoint_returns_schema_valid_json_text() -> None:
    request = {
        "model": "local-dev-seasonality-model",
        "input": [{"role": "user", "content": "analyze"}],
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "structured_output",
                "schema": {"type": "object"},
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
    assert output == {
        "assessment": "ALIGNED",
        "confidence": 0.96,
        "explanation": "Deterministic local development analysis.",
        "risk_flags": [],
    }


def test_mcp_requires_broker_token_and_exposes_only_inventory_tools() -> None:
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
        "Authorization": f"Bearer {REQUEST_ACCESS_TOKEN}",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_mcp_app(), base_url="http://127.0.0.1:18081") as client:
        unauthorized = client.post(
            "/mcp", json=init, headers={"Accept": headers["Accept"]}
        )
        assert unauthorized.status_code == 401
        initialized = client.post("/mcp", json=init, headers=headers)
        assert initialized.status_code == 200
        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )

    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {
        "query_inventory_summary",
        "query_inventory_by_warehouse",
        "query_purchase_in_transit_details",
    }


def test_local_assets_are_closed_valid_and_explicitly_non_production(tmp_path: Path) -> None:
    skill = build_skill_document(
        snapshot_time="2026-08-24T00:00:00Z",
        tenant_id="tenant-local-dev",
        marketplace="US",
        sku="SKU-LOCAL-1",
    )
    assert validate_skill_input(skill, runtime_tenant_id="tenant-local-dev") == []
    assert len(skill["inputs"]["seasonality_profile"]["factors"]) == 730

    assets = write_local_dev_assets(tmp_path)
    assert assets.deployment_config.is_file()
    assert assets.plugin_policy.is_file()
    assert assets.model_schemas.is_file()
    assert assets.tls_ca.is_file()
    assert assets.tls_cert.is_file()
    assert assets.tls_key.is_file()
    assert assets.skill_root.joinpath("SKU-LOCAL-1.json").is_file()
    policy = json.loads(assets.plugin_policy.read_text(encoding="utf-8"))
    assert "${" not in policy["plugins"][0]["package_digest"]
    environment = json.loads(assets.environment.read_text(encoding="utf-8"))
    assert environment["APP_ENV"] == "local_dev"
    assert environment["LOCAL_DEV_E2E"] == "true"
    assert environment["CREDENTIAL_BROKER_URL"] == "http://127.0.0.1:18082/v1/resolve"
    assert environment["MCP_ENDPOINT"] == "http://127.0.0.1:18081/mcp"
    assert environment["OPENAI_ENDPOINT"] == "https://127.0.0.1:18080/v1"
