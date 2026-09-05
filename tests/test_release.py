from __future__ import annotations

import hashlib
from importlib import metadata
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
from ebiz_runtime_contracts import canonical_json_bytes
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


def _installed_contract_root(distribution_name: str, import_root: str) -> Path:
    distribution = metadata.distribution(distribution_name)
    return Path(str(distribution.locate_file(f"{import_root}/contracts"))).resolve(strict=True)


AGENTS = Path("C:/ebizhub/worktrees/ebiz-agents-supply-chain-v4")
CONTRACT_ROOTS = {
    "inventory.core": _installed_contract_root(
        "ebiz-capability-inventory-catalog", "ebiz_capability_inventory_catalog"
    ),
    "commerce-sales.analytics": _installed_contract_root(
        "ebiz-capability-commerce-sales-catalog", "ebiz_capability_commerce_sales_catalog"
    ),
    "supply-chain.planning": _installed_contract_root(
        "ebiz-capability-supply-chain", "ebiz_capability_supply_chain"
    ),
}
WORKFLOW_VERSION_ID = "00000000-0000-4000-8000-000000000040"
AGENT_VERSION_ID = "00000000-0000-4000-8000-000000000041"
WORKFLOW_CHECKSUM = "a" * 64
EXPECTED_PUBLIC_CAPABILITY_PINS = {
    ("catalog.resolve_sku_identity", 2),
    ("inventory.get_total_snapshot", 2),
    ("sales_profit.get_sku_windows", 2),
    ("sales_profit.get_boston_cohort", 1),
    ("supply_chain.resolve_fulfillment_mode", 1),
    ("supply_chain.compute_forecast", 2),
    ("supply_chain.classify_inventory", 2),
    ("supply_chain.route_inventory_action", 1),
    ("supply_chain.build_replenishment_proposal", 2),
    ("supply_chain.build_clearance_proposal", 1),
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


def _release_payloads(release: object) -> tuple[dict[str, object], dict[str, object]]:
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
        "manifest": {
            "distribution": "ebiz-agent-inventory-supply-chain",
            "distribution_version": "4.0.0",
            "record_digest": release.agent_record_digest,  # type: ignore[attr-defined]
        },
        "workflow_pins": [{"code": "inventory-supply-chain-daily", "version": 4}],
        "capability_pins": [{"code": f"cap-{index}", "version": 1} for index in range(10)],
        "max_hosting_level": "ADVISORY",
    }
    return workflow, agent


def _valid_release_responses(
    workflow: dict[str, object], agent: dict[str, object]
) -> dict[str, tuple[int, dict[str, object]]]:
    manifest = agent["manifest"]
    workflow_pins = [
        {
            "code": "inventory-supply-chain-daily",
            "version": 4,
            "ir_checksum": WORKFLOW_CHECKSUM,
        }
    ]
    capability_pins = [
        {**pin, "content_digest": f"{index + 1:064x}"}
        for index, pin in enumerate(agent["capability_pins"])  # type: ignore[arg-type]
    ]
    published_digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "manifest": manifest,
                "workflow_pins": workflow_pins,
                "capability_pins": capability_pins,
            }
        )
    ).hexdigest()
    draft_digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return {
        "/v1/workflows/drafts": (
            201,
            {
                "version_id": WORKFLOW_VERSION_ID,
                "code": workflow["code"],
                "version": workflow["version"],
                "status": "DRAFT",
                "definition_row_version": 2,
                "resources": workflow["resources"],
            },
        ),
        f"/v1/workflows/versions/{WORKFLOW_VERSION_ID}/validate": (
            200,
            {
                "version_id": WORKFLOW_VERSION_ID,
                "status": "VALIDATED",
                "definition_row_version": 2,
                "checksum": WORKFLOW_CHECKSUM,
            },
        ),
        f"/v1/workflows/versions/{WORKFLOW_VERSION_ID}/publish": (
            200,
            {
                "version_id": WORKFLOW_VERSION_ID,
                "code": workflow["code"],
                "version": workflow["version"],
                "checksum": WORKFLOW_CHECKSUM,
                "definition_row_version": 3,
            },
        ),
        "/v1/agents/drafts": (
            201,
            {
                "version_id": AGENT_VERSION_ID,
                "code": agent["code"],
                "version": agent["version"],
                "status": "DRAFT",
                "content_digest": draft_digest,
                "max_hosting_level": agent["max_hosting_level"],
                "workflow_pins": agent["workflow_pins"],
                "capability_pins": agent["capability_pins"],
                "definition_row_version": 4,
                "row_version": 0,
            },
        ),
        f"/v1/agents/versions/{AGENT_VERSION_ID}/publish": (
            200,
            {
                "version_id": AGENT_VERSION_ID,
                "code": agent["code"],
                "version": agent["version"],
                "status": "PUBLISHED",
                "content_digest": published_digest,
                "max_hosting_level": agent["max_hosting_level"],
                "workflow_pins": workflow_pins,
                "capability_pins": capability_pins,
                "definition_row_version": 5,
                "row_version": 1,
            },
        ),
    }


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
    actual_pins = {
        (capability.code, capability.version)
        for publication in publications
        for capability in publication.capabilities
    }
    assert actual_pins == EXPECTED_PUBLIC_CAPABILITY_PINS
    payload = build_agent_draft_payload(config.supply_chain_release, publications)
    AgentDraftRequest.model_validate(payload)
    assert payload["code"] == "inventory-supply-chain"
    assert payload["version"] == 4
    assert payload["workflow_pins"] == [{"code": "inventory-supply-chain-daily", "version": 4}]
    assert {
        (item["code"], item["version"]) for item in payload["capability_pins"]
    } == EXPECTED_PUBLIC_CAPABILITY_PINS
    assert len(payload["capability_pins"]) == len(EXPECTED_PUBLIC_CAPABILITY_PINS)
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
        "openai.responses": "0.1.1",
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

    release = release_config(tmp_path).supply_chain_release
    workflow, agent = _release_payloads(release)
    responses = _valid_release_responses(workflow, agent)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        status, body = responses[request.url.path]
        return httpx.Response(status, json=body)

    monkeypatch.setattr("ebiz_deployment.release.verify_installed_release", lambda value: None)
    async with httpx.AsyncClient(
        base_url="http://runtime.example", transport=httpx.MockTransport(handler)
    ) as client:
        identities = await publish_workflow_and_agent(
            client,
            release=release,
            workflow_payload=workflow,
            agent_payload=agent,
        )

    assert identities == (WORKFLOW_VERSION_ID, AGENT_VERSION_ID)
    assert calls == [
        "/v1/workflows/drafts",
        f"/v1/workflows/versions/{WORKFLOW_VERSION_ID}/validate",
        f"/v1/workflows/versions/{WORKFLOW_VERSION_ID}/publish",
        "/v1/agents/drafts",
        f"/v1/agents/versions/{AGENT_VERSION_ID}/publish",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["agent_code", "agent_version", "manifest_distribution", "manifest_version", "workflow_pin"],
)
async def test_release_rejects_inexact_agent_input_before_any_runtime_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    release = release_config(tmp_path).supply_chain_release
    workflow, agent = _release_payloads(release)
    if case == "agent_code":
        agent["code"] = "other-agent"
    elif case == "agent_version":
        agent["version"] = 5
    elif case == "manifest_distribution":
        agent["manifest"]["distribution"] = "other-distribution"  # type: ignore[index]
    elif case == "manifest_version":
        agent["manifest"]["distribution_version"] = "5.0.0"  # type: ignore[index]
    else:
        agent["workflow_pins"] = [{"code": "other-workflow", "version": 4}]
    monkeypatch.setattr("ebiz_deployment.release.verify_installed_release", lambda value: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Runtime call: {request.url.path}")

    async with httpx.AsyncClient(
        base_url="http://runtime.example", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="Agent draft differs"):
            await publish_workflow_and_agent(
                client,
                release=release,
                workflow_payload=workflow,
                agent_payload=agent,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "field", "bad_value"),
    [
        ("workflow_draft", "version_id", "not-a-uuid"),
        ("workflow_draft", "code", "other-workflow"),
        ("workflow_draft", "version", 5),
        ("workflow_draft", "status", "PUBLISHED"),
        ("workflow_draft", "definition_row_version", 3),
        ("workflow_validate", "version_id", "00000000-0000-4000-8000-000000000099"),
        ("workflow_validate", "status", "DRAFT"),
        ("workflow_validate", "checksum", "not-a-digest"),
        ("workflow_validate", "definition_row_version", 3),
        ("workflow_publish", "version_id", "00000000-0000-4000-8000-000000000099"),
        ("workflow_publish", "code", "other-workflow"),
        ("workflow_publish", "version", 5),
        ("workflow_publish", "checksum", "b" * 64),
        ("workflow_publish", "definition_row_version", 2),
    ],
)
async def test_release_rejects_inexact_workflow_publication_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    field: str,
    bad_value: object,
) -> None:
    release = release_config(tmp_path).supply_chain_release
    workflow, agent = _release_payloads(release)
    responses = _valid_release_responses(workflow, agent)
    paths = {
        "workflow_draft": "/v1/workflows/drafts",
        "workflow_validate": f"/v1/workflows/versions/{WORKFLOW_VERSION_ID}/validate",
        "workflow_publish": f"/v1/workflows/versions/{WORKFLOW_VERSION_ID}/publish",
    }
    responses[paths[endpoint]][1][field] = bad_value
    monkeypatch.setattr("ebiz_deployment.release.verify_installed_release", lambda value: None)

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = responses[request.url.path]
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        base_url="http://runtime.example", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="Workflow"):
            await publish_workflow_and_agent(
                client,
                release=release,
                workflow_payload=workflow,
                agent_payload=agent,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "field", "bad_value"),
    [
        ("agent_draft", "version_id", "not-a-uuid"),
        ("agent_draft", "code", "other-agent"),
        ("agent_draft", "version", 5),
        ("agent_draft", "status", "PUBLISHED"),
        ("agent_draft", "content_digest", "b" * 64),
        ("agent_draft", "workflow_pins", [{"code": "other-workflow", "version": 4}]),
        ("agent_draft", "capability_pins", []),
        ("agent_draft", "definition_row_version", -1),
        ("agent_draft", "row_version", -1),
        ("agent_publish", "version_id", "00000000-0000-4000-8000-000000000099"),
        ("agent_publish", "code", "other-agent"),
        ("agent_publish", "version", 5),
        ("agent_publish", "status", "DRAFT"),
        ("agent_publish", "content_digest", "b" * 64),
        (
            "agent_publish",
            "workflow_pins",
            [{"code": "other-workflow", "version": 4, "ir_checksum": WORKFLOW_CHECKSUM}],
        ),
        ("agent_publish", "capability_pins", []),
        ("agent_publish", "definition_row_version", 4),
        ("agent_publish", "row_version", 0),
    ],
)
async def test_release_rejects_inexact_agent_publication_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    field: str,
    bad_value: object,
) -> None:
    release = release_config(tmp_path).supply_chain_release
    workflow, agent = _release_payloads(release)
    responses = _valid_release_responses(workflow, agent)
    paths = {
        "agent_draft": "/v1/agents/drafts",
        "agent_publish": f"/v1/agents/versions/{AGENT_VERSION_ID}/publish",
    }
    responses[paths[endpoint]][1][field] = bad_value
    monkeypatch.setattr("ebiz_deployment.release.verify_installed_release", lambda value: None)

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = responses[request.url.path]
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        base_url="http://runtime.example", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="Agent"):
            await publish_workflow_and_agent(
                client,
                release=release,
                workflow_payload=workflow,
                agent_payload=agent,
            )


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
