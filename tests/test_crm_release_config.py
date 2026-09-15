"""R1-10: the deployment config opens exactly one CRM seam - four providers, one plugin,
a crm_release block - and the shipped CRM example config is that seam."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_config import deployment_document, deployment_env, load_module, write_runtime_policy
from test_crm_gateway_composition import crm_plugin, crm_release

from ebiz_deployment.config import CRM_READ_TOOL_BY_OPERATION

ROOT = Path(__file__).resolve().parents[1]


def crm_files(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    policy_path = tmp_path / "runtime-policy.json"
    write_runtime_policy(policy_path, tmp_path)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["plugins"].append(crm_plugin())
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    document = deployment_document(policy_path)
    document["credential_broker"]["allowed_provider_ids"].append("yeaher.crm")
    mcp = document["base_ai_providers"][0]
    mcp["config"]["allowed_tools"] = sorted(
        mcp["config"]["allowed_tools"] + list(CRM_READ_TOOL_BY_OPERATION.values())
    )
    document["base_ai_providers"].append(
        {
            "provider_id": "yeaher.crm",
            "package_name": "ebiz-adapter-crm",
            "package_version": "0.1.0",
            "record_digest": "c" * 64,
            "entry_point_group": "base_ai.provider_factories",
            "entry_point_value": "ebiz_adapter_crm:CrmProviderFactory",
            "api_version": "v1",
            "enabled_operations": list(CRM_READ_TOOL_BY_OPERATION),
            "egress_hosts": [],
            "secret_names": [],
            "config": {"mcp": {"tools": CRM_READ_TOOL_BY_OPERATION}},
        }
    )
    document["crm_release"] = crm_release()
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    return config_path, policy_path, deployment_env(policy_path)


def test_four_providers_one_crm_plugin_and_the_crm_release_block_load(tmp_path: Path) -> None:
    config_path, _, env = crm_files(tmp_path)
    config = load_module().load_deployment_config(config_path, env)
    assert [item.provider_id for item in config.base_ai_providers] == [
        "mcp.streamable_http",
        "openai.responses",
        "yeaher.crm",
        "yeaher.erp",
    ]
    assert config.credential_broker.allowed_provider_ids == (
        "mcp.streamable_http",
        "yeaher.crm",
        "yeaher.erp",
    )
    assert {plugin.plugin_id for plugin in config.runtime_plugin_policy.plugins} == {
        "supply-chain-planning",
        "ebizhub.crm-agent",
    }
    release = config.crm_release
    assert release is not None
    assert (release.agent_id, release.agent_distribution, release.agent_distribution_version) == (
        "crm",
        "ebiz-agent-crm",
        "2.0.0",
    )
    assert (release.advise_workflow.code, release.advise_workflow.version) == (
        "crm-case-advise-on-demand",
        1,
    )
    assert [(item.set_id, item.version) for item in release.capability_sets] == [
        ("crm", 2),
        ("crm-advise", 1),
    ]
    assert config.supply_chain_release.streaming_bff.migration_head == (
        "0009_conversation_profile_index"
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d, p: d.pop("crm_release"), "crm_release must be present exactly when"),
        (lambda d, p: d["crm_release"]["capability_sets"].pop(), "capability_sets"),
        (
            lambda d, p: d["crm_release"]["capability_sets"][0].update(
                {"distribution_version": "2.1.0"}
            ),
            "ebiz-agent-crm 2.0.0 wheel",
        ),
        (
            lambda d, p: d["crm_release"]["capability_sets"][1].update({"record_digest": "9" * 64}),
            "agent wheel RECORD digest",
        ),
        (
            lambda d, p: d["crm_release"]["advise_workflow"].update({"version": 2}),
            "advise_workflow",
        ),
        (lambda d, p: d["crm_release"].update({"agent_id": "crm-advisor"}), "agent_id"),
        (
            lambda d, p: p["plugins"].append(
                crm_plugin("ebizhub.crm-advisor", "crm_advisor.plugin:factory")
            ),
            "unapproved plugin set",
        ),
        (
            lambda d, p: p["plugins"][-1].update({"entry_point": "crm_advisor.plugin:factory"}),
            "exact network-free ebizhub.crm-agent pin",
        ),
    ],
)
def test_crm_release_and_plugin_pins_fail_closed(tmp_path: Path, mutate, message: str) -> None:
    config_path, policy_path, env = crm_files(tmp_path)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    mutate(document, policy)
    config_path.write_text(json.dumps(document), encoding="utf-8")
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_module().load_deployment_config(config_path, env)


def test_crm_release_without_the_crm_provider_is_refused(tmp_path: Path) -> None:
    policy_path = tmp_path / "runtime-policy.json"
    write_runtime_policy(policy_path, tmp_path)
    document = deployment_document(policy_path)
    document["crm_release"] = crm_release()
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="crm_release must be present exactly when"):
        load_module().load_deployment_config(config_path, deployment_env(policy_path))


def test_shipped_crm_example_config_is_the_two_profile_deployment(tmp_path: Path) -> None:
    policy_path = tmp_path / "runtime-plugin-policy.crm.json"
    policy_path.write_text(
        (ROOT / "config/runtime-plugin-policy.crm.example.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    env = {
        **deployment_env(policy_path),
        "RUNTIME_PLUGIN_POLICY_PATH": str(policy_path),
        "SUPPLY_CHAIN_PLANNING_RECORD_DIGEST": "d" * 64,
        "SUPPLY_CHAIN_AGENT_RECORD_DIGEST": "1" * 64,
        "INVENTORY_CATALOG_RECORD_DIGEST": "2" * 64,
        "COMMERCE_SALES_CATALOG_RECORD_DIGEST": "3" * 64,
        "SUPPLY_CHAIN_WORKFLOW_DIGEST": "4" * 64,
        "SUPPLY_CHAIN_REGISTRY_GRAPH_DIGEST": "5" * 64,
        "SUPPLY_CHAIN_PUBLICATION_PLAN_DIGEST": "6" * 64,
        "CRM_ADAPTER_RECORD_DIGEST": "7" * 64,
        "CRM_AGENT_RECORD_DIGEST": "8" * 64,
        "CRM_ADVISOR_WORKFLOW_DIGEST": "9" * 64,
        "CRM_POLICY_VERSION": "crm-policy-v1",
        "MCP_PACKAGE_VERSION": "0.1.0",
        "CREDENTIAL_BROKER_URL": "https://credentials.example.com/v1/resolve",
        "MCP_ENDPOINT": "https://mcp.example.com/mcp",
        "MCP_EGRESS_HOST": "mcp.example.com",
        "OPENAI_ENDPOINT": "https://model.example.com/v1",
        "OPENAI_EGRESS_HOST": "model.example.com",
        "ERP_PACKAGE_VERSION": "0.2.0",
        "OPENAI_ADAPTER_PACKAGE_VERSION": "0.1.1",
    }
    config = load_module().load_deployment_config(ROOT / "config/deployment.crm.example.json", env)
    assert {item.provider_id for item in config.base_ai_providers} == {
        "mcp.streamable_http",
        "openai.responses",
        "yeaher.crm",
        "yeaher.erp",
    }
    assert config.crm_release is not None
    assert config.crm_release.advise_workflow.digest == "9" * 64
    assert {plugin.plugin_id for plugin in config.runtime_plugin_policy.plugins} == {
        "supply-chain-planning",
        "ebizhub.crm-agent",
    }
