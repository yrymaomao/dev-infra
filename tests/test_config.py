from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

READ_TOOLS = [
    "query_inventory_by_warehouse",
    "query_inventory_summary",
    "query_purchase_in_transit_details",
]


def write_runtime_policy(path: Path, skill_root: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "plugin_id": "ebizhub.inventory-supply-chain",
                        "version": "1.0.0",
                        "package_name": "ebiz-agent-inventory-supply-chain",
                        "entry_point": "inventory_supply_chain.plugin:factory",
                        "package_digest": "${SUPPLY_CHAIN_RECORD_DIGEST}",
                        "permissions": [
                            "replenishment.preview",
                            "supply_chain.compute",
                            "supply_chain.preview",
                            "supply_chain.skill.read",
                        ],
                        "network_targets": [],
                        "secret_names": [],
                        "config": {"skill_root": str(skill_root)},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def deployment_document(runtime_policy: Path) -> dict[str, object]:
    return {
        "schema_version": "1",
        "runtime": {
            "supported_api_version": "ebizhub.runtime/v1",
            "plugin_policy_path": str(runtime_policy),
        },
        "secrets": {
            "allowed_env": {
                "broker_client_token": "DEPLOY_BROKER_CLIENT_TOKEN",
                "openai_api_key": "DEPLOY_OPENAI_API_KEY",
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
                    "inventory.get_total_snapshot",
                    "sales_profit.get_sku_windows",
                ],
                "egress_hosts": ["cockpit.example.com"],
                "secret_names": [],
                "config": {
                    "cockpit": {
                        "base_url": "https://cockpit.example.com",
                        "network": {
                            "timeout_seconds": 30,
                            "connect_timeout_seconds": 10,
                            "max_connections": 100,
                            "verify_tls": True,
                        },
                    },
                    "mcp": {"tools": {"inventory.get_total_snapshot": "query_inventory_summary"}},
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
                "secret_names": ["openai_api_key"],
                "config": {
                    "api_key_secret_name": "openai_api_key",
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
    }


def deployment_env(runtime_policy: Path) -> dict[str, str]:
    return {
        "APP_PLUGIN_POLICY_PATH": str(runtime_policy.resolve()),
        "SUPPLY_CHAIN_RECORD_DIGEST": "d" * 64,
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
    assert config.runtime_plugin_policy.plugins[0].plugin_id == ("ebizhub.inventory-supply-chain")
    assert config.base_ai_providers[0].package_digest == "a" * 64


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
