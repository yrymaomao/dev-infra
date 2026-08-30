"""Protocol tests for the loopback-only LOCAL_DEV_E2E providers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ebiz_deployment.local_dev_assets import build_skill_document, write_local_dev_assets
from ebiz_deployment.local_dev_services import (
    BROKER_AUTH_TOKEN,
    CREDENTIAL_REF,
    OPENAI_API_KEY,
    REQUEST_ACCESS_TOKEN,
    create_mcp_app,
    create_provider_app,
)


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


def test_mcp_requires_broker_token_and_exposes_only_supply_chain_v4_read_tools() -> None:
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

    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {
        "query_inventory_summary",
        "query_sku_identity",
        "query_sku_sales_profit_windows",
        "query_sku_sales_profit_windows_batch",
        "query_sku_boston_cohort",
    }


def test_local_assets_are_closed_valid_and_explicitly_non_production(
    tmp_path: Path, local_fixture_install: tuple[Path, ...]
) -> None:
    skill = build_skill_document(
        snapshot_time="2026-08-24T00:00:00Z",
        tenant_id="tenant-local-dev",
        market_scope="NA_COMPANY",
        sku="SKU-LOCAL-1",
    )
    assert skill["policy_version"] == "local-dev-v4"
    assert len(skill["seasonality_profile"]["monthly_indices"]) == 12
    assert sum(item["index"] for item in skill["seasonality_profile"]["monthly_indices"]) == 12

    assets = write_local_dev_assets(tmp_path, fixture_search_paths=local_fixture_install)
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
    assert environment["SUPPLY_CHAIN_SKILL_FILE_SHA256"] == hashlib.sha256(
        Path(environment["SUPPLY_CHAIN_SKILL_FILE"]).read_bytes()
    ).hexdigest()
    assert environment["SUPPLY_CHAIN_RUN_ID"] == "supply-chain-v4-local-1"
    assert environment["SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT"] == "5"
    targets = set(environment["APP_CONNECTOR_TARGETS"].split(","))
    assert targets == {
        "yeaher.erp@0.1.0",
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
    assert "deployment.fixture.governed-artifact@1.0.0" not in field_policies
    assert "COCKPIT_ENDPOINT" not in environment


def test_local_asset_generation_is_idempotent_without_importing_agent(
    tmp_path: Path, local_fixture_install: tuple[Path, ...]
) -> None:
    first = write_local_dev_assets(tmp_path, fixture_search_paths=local_fixture_install)
    second = write_local_dev_assets(tmp_path, fixture_search_paths=local_fixture_install)

    assert first.environment == second.environment
    assert second.environment.is_file()


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
