from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

READ_TOOLS = [
    "query_inventory_summary",
    "query_sku_boston_cohort",
    "query_sku_identity",
    "query_sku_sales_profit_windows",
    "query_sku_sales_profit_windows_batch",
]


def write_runtime_policy(path: Path, skill_root: Path) -> None:
    del skill_root
    path.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "plugin_id": "supply-chain-planning",
                        "version": "2.0.0",
                        "package_name": "ebiz-capability-supply-chain",
                        "entry_point": "ebiz_capability_supply_chain.plugin:factory",
                        "package_digest": "${SUPPLY_CHAIN_PLANNING_RECORD_DIGEST}",
                        "permissions": ["supply_chain.preview"],
                        "network_targets": [],
                        "secret_names": [],
                        "config": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def deployment_document(runtime_policy: Path) -> dict[str, object]:
    return {
        "schema_version": "2",
        "runtime": {
            "supported_api_version": "ebizhub.runtime/v1",
            "plugin_policy_path": str(runtime_policy),
        },
        "secrets": {
            "allowed_env": {
                "api_key": "DEPLOY_OPENAI_API_KEY",
                "broker_client_token": "DEPLOY_BROKER_CLIENT_TOKEN",
            }
        },
        "credential_broker": {
            "url": "https://credentials.example.com/v1/resolve",
            "auth_secret_name": "broker_client_token",
            "allowed_provider_ids": ["mcp.streamable_http", "yeaher.erp"],
            "timeout_seconds": 2.0,
        },
        "base_ai_providers": [
            {
                "provider_id": "mcp.streamable_http",
                "package_name": "ebiz-adapter-mcp",
                "package_version": "0.1.0",
                "record_digest": "${MCP_RECORD_DIGEST}",
                "entry_point_group": "base_ai.provider_factories",
                "entry_point_value": "ebiz_adapter_mcp:McpProviderFactory",
                "api_version": "streamable-http/1",
                "enabled_operations": ["tools.call", "tools.list"],
                "egress_hosts": ["mcp.example.com"],
                "secret_names": [],
                "config": {
                    "server_name": "erp-read",
                    "url": "https://mcp.example.com/mcp",
                    "allowed_tools": READ_TOOLS,
                    "auth_profile": "X_MCP_KEY",
                    "network": {
                        "timeout_seconds": 30,
                        "connect_timeout_seconds": 10,
                        "max_connections": 100,
                        "verify_tls": True,
                    },
                },
            },
            {
                "provider_id": "yeaher.erp",
                "package_name": "ebiz-adapter-erp",
                "package_version": "0.1.0",
                "record_digest": "${ERP_RECORD_DIGEST}",
                "entry_point_group": "base_ai.provider_factories",
                "entry_point_value": "ebiz_adapter_erp:ErpProviderFactory",
                "api_version": "v1",
                "enabled_operations": [
                    "catalog.resolve_sku_identity",
                    "inventory.get_total_snapshot",
                    "sales_profit.get_boston_cohort",
                    "sales_profit.get_sku_windows",
                ],
                "egress_hosts": [],
                "secret_names": [],
                "config": {
                    "mcp": {
                        "tools": {
                            "catalog.resolve_sku_identity": "query_sku_identity",
                            "inventory.get_total_snapshot": "query_inventory_summary",
                            "sales_profit.get_boston_cohort": "query_sku_boston_cohort",
                            "sales_profit.get_sku_windows": "query_sku_sales_profit_windows",
                        }
                    },
                },
            },
            {
                "provider_id": "openai.responses",
                "package_name": "ebiz-adapter-model-openai",
                "package_version": "0.1.0",
                "record_digest": "${OPENAI_RECORD_DIGEST}",
                "entry_point_group": "base_ai.provider_factories",
                "entry_point_value": "ebiz_adapter_model_openai:OpenAIProviderFactory",
                "api_version": "responses/v1",
                "enabled_operations": ["responses.create_structured"],
                "egress_hosts": ["api.openai.com"],
                "secret_names": ["api_key"],
                "config": {
                    "api_key_secret_name": "api_key",
                    "base_url": "https://api.openai.com/v1",
                    "enabled_operations": ["responses.create_structured"],
                    "network": {
                        "timeout_seconds": 30,
                        "connect_timeout_seconds": 10,
                        "max_connections": 100,
                        "verify_tls": True,
                    },
                },
            },
        ],
        "supply_chain_release": {
            "agent_id": "inventory-supply-chain",
            "agent_version": 4,
            "agent_distribution": "ebiz-agent-inventory-supply-chain",
            "agent_distribution_version": "4.0.0",
            "agent_record_digest": "${SUPPLY_CHAIN_AGENT_RECORD_DIGEST}",
            "workflow_code": "inventory-supply-chain-daily",
            "workflow_version": 4,
            "workflow_artifact_digest": "${SUPPLY_CHAIN_WORKFLOW_DIGEST}",
            "capability_sets": [
                {
                    "set_id": "inventory.core",
                    "version": 2,
                    "distribution_name": "ebiz-capability-inventory-catalog",
                    "distribution_version": "2.0.0",
                    "record_digest": "${INVENTORY_CATALOG_RECORD_DIGEST}",
                },
                {
                    "set_id": "commerce-sales.analytics",
                    "version": 2,
                    "distribution_name": "ebiz-capability-commerce-sales-catalog",
                    "distribution_version": "2.0.0",
                    "record_digest": "${COMMERCE_SALES_CATALOG_RECORD_DIGEST}",
                },
                {
                    "set_id": "supply-chain.planning",
                    "version": 2,
                    "distribution_name": "ebiz-capability-supply-chain",
                    "distribution_version": "2.0.0",
                    "record_digest": "${SUPPLY_CHAIN_PLANNING_RECORD_DIGEST}",
                },
            ],
            "provider_versions": {
                "yeaher.erp": "0.1.0",
                "supply-chain-planning.fulfillment-resolver": "2.0.0",
                "supply-chain-planning.forecast-engine": "2.0.0",
                "supply-chain-planning.classification-engine": "2.0.0",
                "supply-chain-planning.action-router": "2.0.0",
                "supply-chain-planning.replenishment-engine": "2.0.0",
                "supply-chain-planning.clearance-engine": "2.0.0",
            },
        },
    }


def deployment_env(runtime_policy: Path) -> dict[str, str]:
    return {
        "APP_PLUGIN_POLICY_PATH": str(runtime_policy.resolve()),
        "SUPPLY_CHAIN_PLANNING_RECORD_DIGEST": "d" * 64,
        "SUPPLY_CHAIN_AGENT_RECORD_DIGEST": "e" * 64,
        "SUPPLY_CHAIN_WORKFLOW_DIGEST": "f" * 64,
        "INVENTORY_CATALOG_RECORD_DIGEST": "1" * 64,
        "COMMERCE_SALES_CATALOG_RECORD_DIGEST": "2" * 64,
        "MCP_RECORD_DIGEST": "a" * 64,
        "ERP_RECORD_DIGEST": "b" * 64,
        "OPENAI_RECORD_DIGEST": "c" * 64,
        "DEPLOY_BROKER_CLIENT_TOKEN": "broker-secret-value",
        "DEPLOY_OPENAI_API_KEY": "model-secret-value",
    }


def load_module() -> object:
    return importlib.import_module("ebiz_deployment.config")


def test_loads_strict_complete_read_only_deployment(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    runtime_policy = tmp_path / "runtime-policy.json"
    write_runtime_policy(runtime_policy, skill_root)
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(deployment_document(runtime_policy)), encoding="utf-8")

    config = load_module().load_deployment_config(config_path, deployment_env(runtime_policy))

    assert [item.provider_id for item in config.base_ai_providers] == [
        "mcp.streamable_http",
        "openai.responses",
        "yeaher.erp",
    ]
    assert config.runtime_plugin_policy.plugins[0].plugin_id == "supply-chain-planning"
    assert "cockpit" not in json.dumps(config.model_dump(mode="json")).lower()
    assert config.supply_chain_release.agent_version == 4
    assert [item.set_id for item in config.supply_chain_release.capability_sets] == [
        "commerce-sales.analytics",
        "inventory.core",
        "supply-chain.planning",
    ]
    assert config.base_ai_providers[0].package_digest == "a" * 64


def test_local_fixture_policy_is_allowed_only_for_deterministic_local(tmp_path: Path) -> None:
    runtime_policy = tmp_path / "runtime-policy.json"
    write_runtime_policy(runtime_policy, tmp_path)
    policy = json.loads(runtime_policy.read_text(encoding="utf-8"))
    policy["plugins"].append(
        {
            "plugin_id": "deployment.fixture.governed-artifact",
            "version": "1.0.0",
            "package_name": "ebiz-deployment-local-evidence-fixture",
            "entry_point": "ebiz_deployment_local_fixture.plugin:factory",
            "package_digest": "3" * 64,
            "permissions": ["deployment.fixture.read"],
            "network_targets": [],
            "secret_names": [],
            "config": {},
        }
    )
    runtime_policy.write_text(json.dumps(policy), encoding="utf-8")
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(deployment_document(runtime_policy)), encoding="utf-8")
    local_env = {
        **deployment_env(runtime_policy),
        "APP_ENV": "local_dev",
        "LOCAL_DEV_E2E": "true",
    }

    loaded = load_module().load_deployment_config(config_path, local_env)
    assert {item.plugin_id for item in loaded.runtime_plugin_policy.plugins} == {
        "deployment.fixture.governed-artifact",
        "supply-chain-planning",
    }
    with pytest.raises(ValueError, match="unapproved plugin set"):
        load_module().load_deployment_config(
            config_path,
            {**deployment_env(runtime_policy), "APP_ENV": "production"},
        )


@pytest.mark.parametrize(
    ("mutation", "expected_name"),
    [
        (lambda document: document.update({"unexpected": True}), "unexpected"),
        (
            lambda document: document["base_ai_providers"][0].update(  # type: ignore[index,union-attr]
                {"record_digest": ""}
            ),
            "record_digest",
        ),
        (
            lambda document: document["base_ai_providers"][0]["config"].update(  # type: ignore[index,union-attr]
                {"allowed_tools": [*READ_TOOLS, "create_purchase_order"]}
            ),
            "allowed_tools",
        ),
        (
            lambda document: document["base_ai_providers"][1].update(  # type: ignore[index,union-attr]
                {"enabled_operations": ["inventory.write"]}
            ),
            "enabled_operations",
        ),
    ],
)
def test_rejects_unknown_missing_pin_and_write_surface(
    tmp_path: Path, mutation: object, expected_name: str
) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    runtime_policy = tmp_path / "runtime-policy.json"
    write_runtime_policy(runtime_policy, skill_root)
    document = deployment_document(runtime_policy)
    mutation(document)  # type: ignore[operator]
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_module().load_deployment_config(config_path, deployment_env(runtime_policy))

    assert expected_name in str(error.value)
    assert "broker-secret-value" not in str(error.value)
    assert "model-secret-value" not in str(error.value)


def test_missing_attestation_environment_fails_closed_without_secret_leak(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    runtime_policy = tmp_path / "runtime-policy.json"
    write_runtime_policy(runtime_policy, skill_root)
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(deployment_document(runtime_policy)), encoding="utf-8")
    environ = deployment_env(runtime_policy)
    del environ["MCP_RECORD_DIGEST"]

    with pytest.raises(ValueError) as error:
        load_module().load_deployment_config(config_path, environ)

    assert "MCP_RECORD_DIGEST" in str(error.value)
    assert "broker-secret-value" not in str(error.value)
