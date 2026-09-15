"""Production launcher that injects deployment-owned providers into Runtime."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from agent_runtime.application.provider_composition import ProviderCompositionRoot
from agent_runtime.application.tool_gateway_projection import GatewayPublicationPin
from agent_runtime.cli import api as runtime_api

from .composition import build_provider_composition
from .config import load_deployment_config
from .on_demand_provider import OnDemandProviderComposition
from .tool_gateway import (
    GatewayAuthorityProfile,
    GatewayWorkflowOffer,
    SharedToolGatewayComposition,
)


class RuntimeMain(Protocol):
    def __call__(
        self,
        argv: list[str] | None = None,
        *,
        provider_composition: ProviderCompositionRoot | None = None,
        gateway_composition: object | None = None,
    ) -> int: ...


def launch(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runtime_main: RuntimeMain = runtime_api.main,
) -> int:
    """Validate all deployment-owned inputs before handing control to Runtime."""

    if sys.dont_write_bytecode is not True:
        raise ValueError("Python bytecode writes must be disabled before deployment startup")
    environment = os.environ if environ is None else environ
    config_path = environment.get("EBIZ_DEPLOYMENT_CONFIG", "").strip()
    if not config_path:
        raise ValueError("EBIZ_DEPLOYMENT_CONFIG is required")
    config = load_deployment_config(Path(config_path), environment)
    runtime_policy_path = environment.get("APP_PLUGIN_POLICY_PATH", "").strip()
    if not runtime_policy_path or Path(runtime_policy_path).resolve() != (
        config.runtime.plugin_policy_path.resolve()
    ):
        raise ValueError("APP_PLUGIN_POLICY_PATH must match runtime.plugin_policy_path")
    artifacts = build_provider_composition(config, dict(environment))
    provider_composition: ProviderCompositionRoot = artifacts.root
    if environment.get("SUPPLY_CHAIN_ON_DEMAND_PROVIDER_ENABLED", "false").lower() == "true":
        provider_composition = OnDemandProviderComposition(
            base=provider_composition,
            bff_url=environment.get("SUPPLY_CHAIN_BFF_INTERNAL_URL", ""),
            jwt_secret=environment.get("APP_JWT_SECRET", ""),
            package_digest=environment.get("SUPPLY_CHAIN_ON_DEMAND_PROVIDER_DIGEST", ""),
        )
    gateway_composition = None
    offers = []
    if environment.get("SUPPLY_CHAIN_TOOL_GATEWAY_ENABLED", "false").lower() == "true":
        offers.append(
            GatewayWorkflowOffer(
                pin=GatewayPublicationPin(
                    offer_id=environment.get("BFF_OPENCLAW_OFFER_ID", "supply-chain-on-demand"),
                    kind="workflow",
                    code="inventory-supply-chain-on-demand",
                    version=2,
                    publication_digest=environment.get(
                        "SUPPLY_CHAIN_ON_DEMAND_WORKFLOW_DIGEST", ""
                    ),
                    name="inventory_supply_chain_on_demand",
                    aliases=("supply_chain_analysis",),
                    labels=("inventory", "forecast", "replenishment"),
                ),
                authority=GatewayAuthorityProfile(
                    cid=environment.get("TOOL_GATEWAY_CID", "supply-chain-dev"),
                    credential_ref=environment.get("SUPPLY_CHAIN_CREDENTIAL_REF", ""),
                    scopes=frozenset(
                        {
                            "workflow:start",
                            "runtime:admission",
                            "inventory.read",
                            "sales_profit.read",
                            "supply_chain.preview",
                        }
                    ),
                ),
            )
        )
    if environment.get("CRM_TOOL_GATEWAY_ENABLED", "false").lower() == "true":
        if "yeaher.crm" not in {provider.provider_id for provider in config.base_ai_providers}:
            raise ValueError("CRM Tool Gateway requires the approved read-only CRM provider")
        if config.runtime_plugin_policy is None or not {
            "ebizhub.crm-agent",
            "ebizhub.crm-advisor",
        } <= {plugin.plugin_id for plugin in config.runtime_plugin_policy.plugins}:
            raise ValueError("CRM Tool Gateway requires both CRM plugin pins")
        offers.append(
            GatewayWorkflowOffer(
                pin=GatewayPublicationPin(
                    offer_id="crm-case-advice",
                    kind="workflow",
                    code="crm-case-advise-on-demand",
                    version=1,
                    publication_digest=environment.get("CRM_ADVISOR_WORKFLOW_DIGEST", ""),
                    name="crm_case_advice",
                    aliases=("crm_case_analysis",),
                    labels=("crm", "case", "evidence", "reply-draft"),
                ),
                authority=GatewayAuthorityProfile(
                    cid=environment.get("CRM_TOOL_GATEWAY_CID", "crm-advisor"),
                    credential_ref=environment.get("CRM_CREDENTIAL_REF", ""),
                    scopes=frozenset(
                        {
                            "workflow:start",
                            "runtime:admission",
                            "crm.case.read",
                            "crm.context.read",
                            "crm.evidence.read",
                            "crm.capability.read",
                            "crm.compute",
                            "crm.preview",
                        }
                    ),
                ),
            )
        )
    if offers:
        gateway_composition = SharedToolGatewayComposition(
            tenant_id=environment.get("BFF_OPENCLAW_TENANT_ID", "tenant-local-dev"),
            offers=tuple(offers),
            bff_url=environment.get("SUPPLY_CHAIN_BFF_INTERNAL_URL", ""),
            connector_credential=environment.get("BFF_OPENCLAW_CONNECTOR_CREDENTIAL", ""),
            jwt_key=environment.get("TOOL_GATEWAY_JWT_KEY", ""),
            jwt_issuer=environment.get("TOOL_GATEWAY_JWT_ISSUER", "ebizhub-supply-chain-bff"),
            jwt_audience=environment.get("TOOL_GATEWAY_JWT_AUDIENCE", "ebizhub-tool-gateway"),
            generation_id=environment.get("TOOL_GATEWAY_GENERATION_ID", "supply-chain-v2-dev-1"),
            catalog_revision=environment.get(
                "TOOL_GATEWAY_CATALOG_REVISION", "supply-chain-v2-dev-1"
            ),
        )
    runtime_argv = list(argv) if argv is not None else None
    if gateway_composition is None:
        return runtime_main(runtime_argv, provider_composition=provider_composition)
    return runtime_main(
        runtime_argv,
        provider_composition=provider_composition,
        gateway_composition=gateway_composition,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        return launch(argv)
    except Exception as error:
        print(f"Deployment preflight failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["launch", "main"]
