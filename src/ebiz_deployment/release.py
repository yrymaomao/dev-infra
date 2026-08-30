"""Supply Chain v4 release inputs for Runtime's existing publication surfaces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import httpx
from agent_runtime.api.agents import AgentDraftRequest, AgentPublishRequest
from agent_runtime.api.workflows import DraftRequest, PublishRequest
from agent_runtime.base_ai.composition import (
    BaseAIProviderAttestationPolicy,
    BaseAIProviderDeployment,
)
from agent_runtime.plugins.manifest import AllowedPlugin, PluginHostPolicy
from agent_runtime.registry.capability_manifest import (
    CapabilityCatalogPublication,
    load_capability_publication,
)
from workflow_runtime.authoring.resources import load_resource_bundle

from .attestation import (
    attest_installed_commerce_sales_catalog,
    attest_installed_inventory_catalog,
    attest_installed_supply_chain,
    attest_installed_supply_chain_planning,
)
from .config import DeploymentCompositionConfig, SupplyChainReleaseConfig

_WORKFLOW_RESOURCES = (
    "policies/evidence-policy.yaml",
    "schemas/result.schema.yaml",
    "schemas/seasonality-analysis.schema.yaml",
    "schemas/skill.schema.yaml",
)


def load_public_capability_catalogs(
    release: SupplyChainReleaseConfig,
    contract_roots: Mapping[str, Path],
) -> tuple[CapabilityCatalogPublication, ...]:
    """Load the three governed v2 Catalogs through Runtime's publisher parser."""

    expected_ids = {item.set_id for item in release.capability_sets}
    if set(contract_roots) != expected_ids:
        raise ValueError("contract roots must match the exact Supply Chain v4 capability sets")
    publications: list[CapabilityCatalogPublication] = []
    for pin in release.capability_sets:
        root = contract_roots[pin.set_id].resolve(strict=True)
        publication = load_capability_publication(
            manifest_path=root / "capabilities.yaml",
            contract_root=root,
            provider_versions=release.provider_versions,
        )
        if not isinstance(publication, CapabilityCatalogPublication):
            raise ValueError("Supply Chain v4 requires Runtime v2 Catalog publications")
        if publication.set_id != pin.set_id or publication.version != pin.version:
            raise ValueError("published Catalog identity differs from the release pin")
        publications.append(publication)
    return tuple(sorted(publications, key=lambda item: item.set_id))


def build_agent_draft_payload(
    release: SupplyChainReleaseConfig,
    publications: tuple[CapabilityCatalogPublication, ...],
) -> dict[str, object]:
    """Build the import-only Agent draft request for Runtime's existing HTTP API."""

    if {(item.set_id, item.version) for item in publications} != {
        (item.set_id, item.version) for item in release.capability_sets
    }:
        raise ValueError("Agent draft publications differ from the exact release pins")
    capability_pins: list[dict[str, str | int]] = sorted(
        (
            {"code": capability.code, "version": capability.version}
            for publication in publications
            for capability in publication.capabilities
        ),
        key=lambda item: (str(item["code"]), item["version"]),
    )
    if len(capability_pins) != 10 or len({str(item["code"]) for item in capability_pins}) != 10:
        raise ValueError("Supply Chain v4 requires ten unique exact capability pins")
    return {
        "code": release.agent_id,
        "name": "Supply Chain Expert",
        "version": release.agent_version,
        "manifest": {
            "distribution": release.agent_distribution,
            "distribution_version": release.agent_distribution_version,
            "record_digest": release.agent_record_digest,
        },
        "workflow_pins": [{"code": release.workflow_code, "version": release.workflow_version}],
        "capability_pins": capability_pins,
        "max_hosting_level": "ADVISORY",
    }


def build_capability_publish_commands(
    release: SupplyChainReleaseConfig,
    contract_roots: Mapping[str, Path],
    *,
    policy_path: Path,
    base_ai_attestation_path: Path,
    tenant_id: str,
    actor_id: str,
    trace_id: str,
) -> tuple[tuple[str, ...], ...]:
    """Return exact argv for Runtime's existing attested publisher CLI."""

    if not tenant_id.strip() or not actor_id.strip() or not trace_id.strip():
        raise ValueError("publisher identity and trace inputs are required")
    if set(contract_roots) != {item.set_id for item in release.capability_sets}:
        raise ValueError("contract roots must match the exact Supply Chain v4 capability sets")
    attestation = base_ai_attestation_path.resolve(strict=True)
    commands: list[tuple[str, ...]] = []
    for pin in release.capability_sets:
        root = contract_roots[pin.set_id].resolve(strict=True)
        commands.append(
            (
                "python",
                "-m",
                "agent_runtime.cli.capability_publish",
                "--manifest",
                str(root / "capabilities.yaml"),
                "--contract-root",
                str(root),
                "--policy",
                str(policy_path.resolve(strict=True)),
                "--base-ai-provider-attestation",
                str(attestation),
                "--tenant-id",
                tenant_id,
                "--actor-id",
                actor_id,
                "--trace-id",
                trace_id,
            )
        )
    return tuple(commands)


def build_local_fixture_publish_command(
    *,
    app_env: str,
    local_dev_e2e: bool,
    contract_root: Path,
    policy_path: Path,
    tenant_id: str,
    actor_id: str,
    trace_id: str,
) -> tuple[str, ...]:
    """Build the existing Runtime publisher argv for the local-only fixture Catalog."""

    if app_env != "local_dev" or not local_dev_e2e:
        raise ValueError("fixture publication requires deterministic LOCAL_DEV_E2E")
    if not tenant_id.strip() or not actor_id.strip() or not trace_id.strip():
        raise ValueError("fixture publisher identity and trace inputs are required")
    root = contract_root.resolve(strict=True)
    manifest = root / "capabilities.yaml"
    if not manifest.is_file():
        raise ValueError("local fixture Catalog manifest is missing")
    return (
        "python",
        "-m",
        "agent_runtime.cli.capability_publish",
        "--manifest",
        str(manifest),
        "--contract-root",
        str(root),
        "--policy",
        str(policy_path.resolve(strict=True)),
        "--tenant-id",
        tenant_id,
        "--actor-id",
        actor_id,
        "--trace-id",
        trace_id,
    )


def write_base_ai_provider_attestation(
    config: DeploymentCompositionConfig, path: Path
) -> BaseAIProviderAttestationPolicy:
    """Write Runtime's closed credential-free Base AI attestation document."""

    deployments = tuple(
        BaseAIProviderDeployment(
            provider_id=item.provider_id,
            package_name=item.package_name,
            package_version=item.package_version,
            package_digest=item.package_digest,
            entry_point=item.entry_point_value,
            entry_point_group=item.entry_point_group,
            api_version=item.api_version,
            enabled_operations=item.enabled_operations,
            egress_hosts=item.egress_hosts,
            secret_names=item.secret_names,
            config=item.config,
        )
        for item in config.base_ai_providers
    )
    plugin_policy = PluginHostPolicy(
        plugins=tuple(
            AllowedPlugin(
                plugin_id=item.plugin_id,
                version=item.package_version,
                package_name=item.package_name,
                entry_point=item.entry_point,
                package_digest=item.package_digest,
                permissions=frozenset(item.enabled_operations),
                network_targets=frozenset(item.egress_hosts),
                secret_names=frozenset(item.secret_names),
                config=dict(item.config),
            )
            for item in deployments
        )
    )
    policy = BaseAIProviderAttestationPolicy(
        schema_version="ebizhub.base-ai-provider-attestation/v1",
        deployments=deployments,
        plugin_policy=plugin_policy,
    )
    path.write_text(
        json.dumps(policy.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8"
    )
    return policy


def verify_installed_release(release: SupplyChainReleaseConfig) -> None:
    """Bind every configured RECORD pin to its installed immutable wheel."""

    try:
        actual = {
            "ebiz-agent-inventory-supply-chain": (attest_installed_supply_chain().canonical_digest),
            "ebiz-capability-inventory-catalog": (
                attest_installed_inventory_catalog().canonical_digest
            ),
            "ebiz-capability-commerce-sales-catalog": (
                attest_installed_commerce_sales_catalog().canonical_digest
            ),
            "ebiz-capability-supply-chain": (
                attest_installed_supply_chain_planning().canonical_digest
            ),
        }
    except ValueError:
        raise ValueError("installed Supply Chain v4 release digests are unavailable") from None
    expected = {release.agent_distribution: release.agent_record_digest}
    expected.update(
        {item.distribution_name: item.record_digest for item in release.capability_sets}
    )
    if actual != expected:
        raise ValueError("installed Supply Chain v4 release digests differ from exact pins")


def build_workflow_draft_payload(
    release: SupplyChainReleaseConfig, agent_root: Path
) -> dict[str, object]:
    """Build and validate Runtime's v4 Workflow draft payload from attested resources."""

    root = agent_root.resolve(strict=True)
    source_path = root / "workflows/inventory-supply-chain-daily.yaml"
    source = source_path.read_text(encoding="utf-8")
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != release.workflow_artifact_digest:
        raise ValueError("Workflow source digest differs from the exact release pin")
    payload: dict[str, object] = {
        "code": release.workflow_code,
        "name": "Supply Chain Expert v4",
        "version": release.workflow_version,
        "source_yaml": source,
        "resources": load_resource_bundle(root, _WORKFLOW_RESOURCES),
    }
    DraftRequest.model_validate(payload)
    return payload


async def publish_workflow_and_agent(
    client: httpx.AsyncClient,
    *,
    release: SupplyChainReleaseConfig,
    workflow_payload: dict[str, object],
    agent_payload: dict[str, object],
) -> tuple[str, str]:
    """Use only Runtime's existing Workflow and Agent draft/publish HTTP APIs."""

    verify_installed_release(release)
    if (
        workflow_payload.get("code") != release.workflow_code
        or workflow_payload.get("version") != release.workflow_version
    ):
        raise ValueError("Workflow draft differs from the exact release identity")
    manifest = agent_payload.get("manifest")
    if (
        not isinstance(manifest, dict)
        or manifest.get("record_digest") != release.agent_record_digest
    ):
        raise ValueError("Agent draft differs from the exact attested release")

    workflow_draft = await client.post("/v1/workflows/drafts", json=workflow_payload)
    workflow_draft.raise_for_status()
    workflow_id = workflow_draft.json().get("version_id")
    if not isinstance(workflow_id, str):
        raise ValueError("Workflow draft response omitted version_id")
    validated = await client.post(f"/v1/workflows/versions/{workflow_id}/validate")
    validated.raise_for_status()
    workflow_row = validated.json().get("definition_row_version")
    if not isinstance(workflow_row, int):
        raise ValueError("Workflow validation omitted row version")
    workflow_publish = PublishRequest(expected_row_version=workflow_row).model_dump()
    published = await client.post(
        f"/v1/workflows/versions/{workflow_id}/publish", json=workflow_publish
    )
    published.raise_for_status()
    if published.json().get("checksum") != validated.json().get("checksum"):
        raise ValueError("published Workflow checksum differs from validation")

    AgentDraftRequest.model_validate(agent_payload)
    agent_draft = await client.post("/v1/agents/drafts", json=agent_payload)
    agent_draft.raise_for_status()
    agent_id = agent_draft.json().get("version_id")
    agent_row = agent_draft.json().get("row_version")
    if not isinstance(agent_id, str) or not isinstance(agent_row, int):
        raise ValueError("Agent draft response omitted publication identity")
    agent_publish = AgentPublishRequest(expected_row_version=agent_row).model_dump()
    published_agent = await client.post(
        f"/v1/agents/versions/{agent_id}/publish", json=agent_publish
    )
    published_agent.raise_for_status()
    return workflow_id, agent_id


__all__ = [
    "build_agent_draft_payload",
    "build_capability_publish_commands",
    "build_local_fixture_publish_command",
    "build_workflow_draft_payload",
    "load_public_capability_catalogs",
    "publish_workflow_and_agent",
    "verify_installed_release",
    "write_base_ai_provider_attestation",
]
