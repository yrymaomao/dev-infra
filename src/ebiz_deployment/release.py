"""Supply Chain v4 release inputs for Runtime's existing publication surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from agent_runtime.registry.capability_manifest import (
    CapabilityCatalogPublication,
    load_capability_publication,
)

from .config import SupplyChainReleaseConfig


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
    tenant_id: str,
    actor_id: str,
    trace_id: str,
) -> tuple[tuple[str, ...], ...]:
    """Return auditable argv for Runtime's existing one-shot publisher CLI."""

    if not tenant_id.strip() or not actor_id.strip() or not trace_id.strip():
        raise ValueError("publisher identity and trace inputs are required")
    if set(contract_roots) != {item.set_id for item in release.capability_sets}:
        raise ValueError("contract roots must match the exact Supply Chain v4 capability sets")
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
                str(policy_path.resolve()),
                "--tenant-id",
                tenant_id,
                "--actor-id",
                actor_id,
                "--trace-id",
                trace_id,
            )
        )
    return tuple(commands)


__all__ = [
    "build_agent_draft_payload",
    "build_capability_publish_commands",
    "load_public_capability_catalogs",
]
