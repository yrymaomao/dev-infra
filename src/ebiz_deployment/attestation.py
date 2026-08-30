"""Emit deployment digest inputs from installed, RECORD-attested Adapter wheels."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from base_ai.providers import discover_provider_factory_descriptors

from .record_attestation import (
    InstalledDistributionAttestation,
    attest_installed_distribution,
    attest_installed_resource_distribution,
)

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_PROVIDERS = {
    "mcp.streamable_http": (
        "MCP_RECORD_DIGEST",
        "ebiz-adapter-mcp",
        "ebiz_adapter_mcp:McpProviderFactory",
        "streamable-http/1",
    ),
    "yeaher.erp": (
        "ERP_RECORD_DIGEST",
        "ebiz-adapter-erp",
        "ebiz_adapter_erp:ErpProviderFactory",
        "v1",
    ),
    "openai.responses": (
        "OPENAI_RECORD_DIGEST",
        "ebiz-adapter-model-openai",
        "ebiz_adapter_model_openai:OpenAIProviderFactory",
        "responses/v1",
    ),
}


class Descriptor(Protocol):
    name: str
    distribution_name: str
    distribution_version: str
    distribution_digest: str
    value: str
    api_version: str
    production_eligible: bool


def build_attestation_environment(
    descriptors: Sequence[Descriptor],
    *,
    supply_chain_search_paths: Sequence[Path] | None = None,
    planning_search_paths: Sequence[Path] | None = None,
    inventory_catalog_search_paths: Sequence[Path] | None = None,
    commerce_sales_catalog_search_paths: Sequence[Path] | None = None,
) -> dict[str, str]:
    """Return only digests after checking the installed descriptor identities."""

    supply_chain = attest_installed_supply_chain(search_paths=supply_chain_search_paths)
    by_id = {descriptor.name: descriptor for descriptor in descriptors}
    if len(by_id) != len(descriptors) or set(by_id) != set(_PROVIDERS):
        raise ValueError("Base AI artifact attestation set is incomplete or contains extras")
    planning = attest_installed_supply_chain_planning(search_paths=planning_search_paths)
    inventory = attest_installed_inventory_catalog(search_paths=inventory_catalog_search_paths)
    commerce_sales = attest_installed_commerce_sales_catalog(
        search_paths=commerce_sales_catalog_search_paths
    )
    output = {
        "COMMERCE_SALES_CATALOG_RECORD_DIGEST": commerce_sales.canonical_digest,
        "INVENTORY_CATALOG_RECORD_DIGEST": inventory.canonical_digest,
        "SUPPLY_CHAIN_AGENT_RECORD_DIGEST": supply_chain.canonical_digest,
        "SUPPLY_CHAIN_PLANNING_RECORD_DIGEST": planning.canonical_digest,
    }
    for provider_id, (environment_name, package_name, value, api_version) in _PROVIDERS.items():
        descriptor = by_id[provider_id]
        if (
            descriptor.production_eligible is not True
            or descriptor.distribution_name != package_name
            or descriptor.value != value
            or descriptor.api_version != api_version
            or not descriptor.distribution_version
            or _DIGEST.fullmatch(descriptor.distribution_digest) is None
        ):
            raise ValueError("installed Adapter artifact attestation is not production eligible")
        output[environment_name] = descriptor.distribution_digest
    return dict(sorted(output.items()))


def attest_installed_supply_chain(
    *,
    search_paths: Sequence[Path] | None = None,
) -> InstalledDistributionAttestation:
    """Attest the import-only Supply Chain v4 Agent wheel without importing it."""

    try:
        return attest_installed_resource_distribution(
            distribution_name="ebiz-agent-inventory-supply-chain",
            distribution_version="4.0.0",
            import_root="inventory_supply_chain_agent",
            forbidden_entry_point_group="ebiz_agents.providers",
            search_paths=search_paths,
        )
    except ValueError:
        raise ValueError("Supply Chain distribution attestation failed") from None


def attest_installed_supply_chain_planning(
    *,
    search_paths: Sequence[Path] | None = None,
) -> InstalledDistributionAttestation:
    """Attest the exact public planning Provider wheel."""

    try:
        return attest_installed_distribution(
            distribution_name="ebiz-capability-supply-chain",
            distribution_version="2.0.0",
            entry_point_group="ebiz_agents.providers",
            entry_point_name="supply-chain-planning",
            entry_point_value="ebiz_capability_supply_chain.plugin:factory",
            search_paths=search_paths,
        )
    except ValueError:
        raise ValueError("Supply Chain planning attestation failed") from None


def attest_installed_inventory_catalog(
    *, search_paths: Sequence[Path] | None = None
) -> InstalledDistributionAttestation:
    """Attest the exact inventory.core@2 resource wheel."""

    try:
        return attest_installed_resource_distribution(
            distribution_name="ebiz-capability-inventory-catalog",
            distribution_version="2.0.0",
            import_root="ebiz_capability_inventory_catalog",
            forbidden_entry_point_group="ebiz_agents.providers",
            search_paths=search_paths,
        )
    except ValueError:
        raise ValueError("Inventory Catalog attestation failed") from None


def attest_installed_commerce_sales_catalog(
    *, search_paths: Sequence[Path] | None = None
) -> InstalledDistributionAttestation:
    """Attest the exact commerce-sales.analytics@2 resource wheel."""

    try:
        return attest_installed_resource_distribution(
            distribution_name="ebiz-capability-commerce-sales-catalog",
            distribution_version="2.0.0",
            import_root="ebiz_capability_commerce_sales_catalog",
            forbidden_entry_point_group="ebiz_agents.providers",
            search_paths=search_paths,
        )
    except ValueError:
        raise ValueError("Commerce Sales Catalog attestation failed") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit digest environment inputs from installed production wheels"
    )
    parser.parse_args(argv)
    result = build_attestation_environment(
        cast(Sequence[Descriptor], discover_provider_factory_descriptors()),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "attest_installed_supply_chain",
    "attest_installed_supply_chain_planning",
    "attest_installed_inventory_catalog",
    "attest_installed_commerce_sales_catalog",
    "build_attestation_environment",
    "main",
]
