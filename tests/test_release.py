from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from agent_runtime.api.agents import AgentDraftRequest
from agent_runtime.base_ai.composition import (
    attest_base_ai_provider_versions,
    load_base_ai_provider_attestation_policy,
)
from agent_runtime.registry.capability_manifest import CapabilityPublicationError
from base_ai.providers import discover_provider_factory_descriptors
from test_config import deployment_document, deployment_env, write_runtime_policy

from ebiz_deployment.config import DeploymentCompositionConfig
from ebiz_deployment.release import (
    build_agent_draft_payload,
    build_capability_publish_commands,
    build_local_fixture_publish_command,
    build_workflow_draft_payload,
    load_public_capability_catalogs,
    publish_workflow_and_agent,
    verify_installed_release,
    write_base_ai_provider_attestation,
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


def test_release_uses_runtime_base_ai_attestation_for_all_catalog_commands(
    tmp_path: Path,
) -> None:
    config = release_config(tmp_path)
    policy = tmp_path / "runtime-policy.json"
    attestation_path = tmp_path / "base-ai-provider-attestation.json"
    attestation = write_base_ai_provider_attestation(config, attestation_path)

    commands = build_capability_publish_commands(
        config.supply_chain_release,
        CONTRACT_ROOTS,
        policy_path=policy,
        base_ai_attestation_path=attestation_path,
        tenant_id="tenant-a",
        actor_id="00000000-0000-4000-8000-000000000001",
        trace_id="release-v4",
    )

    assert attestation.schema_version == "ebizhub.base-ai-provider-attestation/v1"
    assert [item.provider_id for item in attestation.deployments] == [
        "mcp.streamable_http",
        "openai.responses",
        "yeaher.erp",
    ]
    raw = attestation_path.read_text(encoding="utf-8")
    assert "broker-secret-value" not in raw
    assert "model-secret-value" not in raw
    assert len(commands) == 3
    assert all("--base-ai-provider-attestation" in command for command in commands)
    assert all(str(attestation_path.resolve()) in command for command in commands)


def test_runtime_attests_the_exact_deployment_base_ai_manifest(tmp_path: Path) -> None:
    policy = tmp_path / "runtime-policy.json"
    write_runtime_policy(policy, tmp_path)
    document = deployment_document(policy)
    descriptors = {item.name: item for item in discover_provider_factory_descriptors()}
    environment = deployment_env(policy)
    environment.update(
        {
            "MCP_RECORD_DIGEST": descriptors["mcp.streamable_http"].distribution_digest,
            "ERP_RECORD_DIGEST": descriptors["yeaher.erp"].distribution_digest,
            "OPENAI_RECORD_DIGEST": descriptors["openai.responses"].distribution_digest,
        }
    )
    config = DeploymentCompositionConfig.model_validate(
        _expand_test_environment(document, environment)
    )
    path = tmp_path / "base-ai-provider-attestation.json"
    write_base_ai_provider_attestation(config, path)

    loaded = load_base_ai_provider_attestation_policy(path)
    versions = attest_base_ai_provider_versions(loaded)

    assert dict(versions) == {
        "mcp.streamable_http": "0.1.0",
        "openai.responses": "0.1.0",
        "yeaher.erp": "0.1.0",
    }


def test_local_fixture_uses_existing_publisher_and_is_guarded(tmp_path: Path) -> None:
    contract_root = Path("packages/local-evidence-fixture/contracts").resolve()
    policy = tmp_path / "fixture-policy.json"
    policy.write_text('{"plugins": []}', encoding="utf-8")

    command = build_local_fixture_publish_command(
        app_env="local_dev",
        local_dev_e2e=True,
        contract_root=contract_root,
        policy_path=policy,
        tenant_id="tenant-a",
        actor_id="00000000-0000-4000-8000-000000000001",
        trace_id="fixture-v1",
    )

    assert command[:3] == ("python", "-m", "agent_runtime.cli.capability_publish")
    assert "--base-ai-provider-attestation" not in command
    with pytest.raises(ValueError, match="LOCAL_DEV_E2E"):
        build_local_fixture_publish_command(
            app_env="real_dev",
            local_dev_e2e=True,
            contract_root=contract_root,
            policy_path=policy,
            tenant_id="tenant-a",
            actor_id="00000000-0000-4000-8000-000000000001",
            trace_id="fixture-v1",
        )


def test_workflow_draft_binds_source_digest_and_all_resources(tmp_path: Path) -> None:
    config = release_config(tmp_path)
    agent_root = AGENTS / "agents/supply-chain"
    workflow = agent_root / "workflows/inventory-supply-chain-daily.yaml"
    release = config.supply_chain_release.model_copy(
        update={"workflow_artifact_digest": hashlib.sha256(workflow.read_bytes()).hexdigest()}
    )

    payload = build_workflow_draft_payload(release, agent_root)

    assert payload["code"] == "inventory-supply-chain-daily"
    assert set(payload["resources"]) == {
        "policies/evidence-policy.yaml",
        "schemas/result.schema.yaml",
        "schemas/seasonality-analysis.schema.yaml",
        "schemas/skill.schema.yaml",
    }
    with pytest.raises(ValueError, match="source digest"):
        build_workflow_draft_payload(
            release.model_copy(update={"workflow_artifact_digest": "0" * 64}), agent_root
        )


def test_installed_release_rejects_caller_supplied_record_digests(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="digests"):
        verify_installed_release(release_config(tmp_path).supply_chain_release)


@pytest.mark.asyncio
async def test_release_uses_existing_workflow_and_agent_publish_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        responses = {
            "/v1/workflows/drafts": (201, {"version_id": "workflow-v4"}),
            "/v1/workflows/versions/workflow-v4/validate": (
                200,
                {"definition_row_version": 2, "checksum": "canonical-v4"},
            ),
            "/v1/workflows/versions/workflow-v4/publish": (
                200,
                {"checksum": "canonical-v4"},
            ),
            "/v1/agents/drafts": (201, {"version_id": "agent-v4", "row_version": 3}),
            "/v1/agents/versions/agent-v4/publish": (200, {"status": "PUBLISHED"}),
        }
        status, body = responses[request.url.path]
        return httpx.Response(status, json=body)

    release = release_config(tmp_path).supply_chain_release
    monkeypatch.setattr("ebiz_deployment.release.verify_installed_release", lambda value: None)
    workflow = {
        "code": "inventory-supply-chain-daily",
        "name": "Supply Chain Expert v4",
        "version": 4,
        "source_yaml": "spec_version: ebizhub.workflow/v1.3\n",
        "resources": {},
    }
    agent = {
        "code": "inventory-supply-chain",
        "name": "Supply Chain Expert",
        "version": 4,
        "manifest": {"record_digest": release.agent_record_digest},
        "workflow_pins": [{"code": "inventory-supply-chain-daily", "version": 4}],
        "capability_pins": [{"code": f"cap-{index}", "version": 1} for index in range(10)],
        "max_hosting_level": "ADVISORY",
    }
    async with httpx.AsyncClient(
        base_url="http://runtime.local", transport=httpx.MockTransport(handler)
    ) as client:
        identities = await publish_workflow_and_agent(
            client,
            release=release,
            workflow_payload=workflow,
            agent_payload=agent,
        )

    assert identities == ("workflow-v4", "agent-v4")
    assert calls == [
        "/v1/workflows/drafts",
        "/v1/workflows/versions/workflow-v4/validate",
        "/v1/workflows/versions/workflow-v4/publish",
        "/v1/agents/drafts",
        "/v1/agents/versions/agent-v4/publish",
    ]


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
