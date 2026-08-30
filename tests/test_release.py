from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime.api.agents import AgentDraftRequest
from agent_runtime.registry.capability_manifest import CapabilityPublicationError
from test_config import deployment_document, deployment_env, write_runtime_policy

from ebiz_deployment.config import DeploymentCompositionConfig
from ebiz_deployment.release import (
    build_agent_draft_payload,
    build_capability_publish_commands,
    load_public_capability_catalogs,
)

AGENTS = Path("C:/ebizhub/worktrees/ebiz-agents-supply-chain-v4")
CONTRACT_ROOTS = {
    "inventory.core": AGENTS / "capabilities/inventory/contracts",
    "commerce-sales.analytics": AGENTS / "capabilities/commerce-sales/contracts",
    "supply-chain.planning": AGENTS / "capabilities/supply-chain/contracts",
}


def release_config(tmp_path: Path) -> DeploymentCompositionConfig:
    policy = tmp_path / "runtime-policy.json"
    write_runtime_policy(policy, tmp_path)
    document = deployment_document(policy)
    return DeploymentCompositionConfig.model_validate(
        _expand_test_environment(document, deployment_env(policy))
    )


def _expand_test_environment(value: object, environ: dict[str, str]) -> object:
    if isinstance(value, dict):
        return {key: _expand_test_environment(item, environ) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_test_environment(item, environ) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return environ[value[2:-1]]
    return value


def test_runtime_publisher_loads_three_public_catalogs_and_exact_capability_pins(
    tmp_path: Path,
) -> None:
    config = release_config(tmp_path)

    publications = load_public_capability_catalogs(config.supply_chain_release, CONTRACT_ROOTS)

    assert [item.set_id for item in publications] == [
        "commerce-sales.analytics",
        "inventory.core",
        "supply-chain.planning",
    ]
    assert [(item.set_id, item.version, len(item.capabilities)) for item in publications] == [
        ("commerce-sales.analytics", 2, 2),
        ("inventory.core", 2, 2),
        ("supply-chain.planning", 2, 6),
    ]
    payload = build_agent_draft_payload(config.supply_chain_release, publications)
    AgentDraftRequest.model_validate(payload)
    assert payload["code"] == "inventory-supply-chain"
    assert payload["version"] == 4
    assert payload["workflow_pins"] == [{"code": "inventory-supply-chain-daily", "version": 4}]
    assert len(payload["capability_pins"]) == 10
    assert payload["capability_pins"] == sorted(
        payload["capability_pins"], key=lambda item: (item["code"], item["version"])
    )


def test_release_uses_existing_runtime_publisher_cli_for_each_catalog(tmp_path: Path) -> None:
    config = release_config(tmp_path)
    policy = tmp_path / "runtime-policy.json"

    commands = build_capability_publish_commands(
        config.supply_chain_release,
        CONTRACT_ROOTS,
        policy_path=policy,
        tenant_id="tenant-a",
        actor_id="00000000-0000-4000-8000-000000000001",
        trace_id="release-v4",
    )

    assert len(commands) == 3
    assert all(
        command[:3] == ("python", "-m", "agent_runtime.cli.capability_publish")
        for command in commands
    )
    assert all("running_app" not in " ".join(command) for command in commands)


def test_runtime_publisher_rejects_the_old_incomplete_catalog_shape(tmp_path: Path) -> None:
    config = release_config(tmp_path)
    root = tmp_path / "contracts"
    root.mkdir()
    (root / "capabilities.yaml").write_text(
        "spec_version: ebizhub.capability-set/v1\n"
        "kind: CapabilitySet\n"
        "set_id: inventory.core\n"
        "version: 2\n"
        "capabilities: []\n",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityPublicationError):
        load_public_capability_catalogs(
            config.supply_chain_release,
            {**CONTRACT_ROOTS, "inventory.core": root},
        )
