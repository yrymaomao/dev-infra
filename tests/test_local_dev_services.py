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
    OPENAI_API_KEY,
    REQUEST_ACCESS_TOKEN,
    create_mcp_app,
    create_provider_app,
)
from ebiz_deployment.record_attestation import attest_installed_distribution


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
        cohort = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "query_sku_boston_cohort",
                    "arguments": {
                        "marketScope": "NA_COMPANY",
                        "snapshotTime": "2026-08-30T12:00:00Z",
                        "sku": "SKU-LOCAL-1",
                        "pageSize": 1000,
                    },
                },
            },
            headers=headers,
        )
        sales_batch = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "query_sku_sales_profit_windows_batch",
                    "arguments": {
                        "marketScope": "NA_COMPANY",
                        "snapshotTime": "2026-08-30T12:00:00Z",
                        "skuCodes": ["SKU-LOCAL-1", "SKU-LOCAL-2"],
                    },
                },
            },
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
    for tool in listed.json()["result"]["tools"]:
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["outputSchema"]["additionalProperties"] is False
    cohort_tool = next(
        tool
        for tool in listed.json()["result"]["tools"]
        if tool["name"] == "query_sku_boston_cohort"
    )
    cohort_input = cohort_tool["inputSchema"]
    assert set(cohort_input["required"]) == {
        "sku",
        "marketScope",
        "snapshotTime",
        "pageSize",
    }
    assert cohort_input["properties"]["pageSize"]["minimum"] == 5
    assert cohort_input["properties"]["pageSize"]["maximum"] == 1000
    assert cohort_input["properties"]["snapshotTime"]["format"] == "date-time"
    cohort_output = cohort_tool["outputSchema"]
    assert cohort_output["properties"]["result"]["additionalProperties"] is False
    member_schema = cohort_output["properties"]["result"]["properties"]["members"]["items"]
    assert member_schema["additionalProperties"] is False
    assert cohort.status_code == 200
    call_result = cohort.json()["result"]
    assert call_result["isError"] is False
    cohort_result = call_result["structuredContent"]["result"]
    assert set(cohort_result) == {
        "status",
        "filterDefinition",
        "totalEligible",
        "cohortSnapshotId",
        "nextCursor",
        "members",
        "dataAsOf",
        "sourceWatermark",
    }
    assert cohort_result["dataAsOf"] == "2026-08-30T12:00:00Z"
    assert cohort_result["sourceWatermark"] == "local-cohort-watermark-1"
    assert cohort_result["nextCursor"] is None
    batch_result = sales_batch.json()["result"]
    assert batch_result["isError"] is False
    batch_document = batch_result["structuredContent"]
    assert set(batch_document) == {
        "contractVersion",
        "statisticsVersion",
        "statisticsDefinition",
        "marketScope",
        "snapshotTime",
        "observedAt",
        "result",
    }
    assert len(batch_document["result"]["items"]) == 2


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
    assert environment["SUPPLY_CHAIN_RUN_ID"] == "supply-chain-v4-local-1"
    assert environment["SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT"] == "5"
    assert environment["SUPPLY_CHAIN_EXPECTED_RESULT_STATUS"] == "COMPLETE"
    assert environment["SUPPLY_CHAIN_SNAPSHOT_TIME"] == "2026-08-31T06:00:00Z"
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
        distribution_version="1.0.0",
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
