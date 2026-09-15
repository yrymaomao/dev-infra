"""Production launcher that injects deployment-owned providers into Runtime."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from agent_runtime.application.provider_composition import ProviderCompositionRoot
from agent_runtime.application.tool_gateway_projection import GatewayPublicationPin
from agent_runtime.cli import api as runtime_api

from .composition import build_provider_composition
from .config import (
    CRM_ADVISE_WORKFLOW,
    CRM_PLUGIN_ID,
    DeploymentCompositionConfig,
    load_deployment_config,
)
from .on_demand_provider import OnDemandProviderComposition
from .openclaw_reception.profile import session_key_prefix_for
from .tool_gateway import (
    GatewayAuthorityProfile,
    GatewayWorkflowOffer,
    SharedToolGatewayComposition,
)

SUPPLY_CHAIN_SCOPES = frozenset(
    {
        "workflow:start",
        "runtime:admission",
        "inventory.read",
        "sales_profit.read",
        "supply_chain.preview",
    }
)
#: Exactly the eight scopes the advise graph needs: admission plus the four CRM
#: reads it binds plus the two in-process provider scopes. No crm.*.write, no
#: crm.message.send, nothing of Supply Chain.
CRM_SCOPES = frozenset(
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
)
CRM_OFFER_ID = "crm-case-advice"
CRM_TOOL_NAME = "crm_case_advice"
CRM_TOOL_ALIASES = ("crm_case_analysis", "crm_case_advise_on_demand")
CRM_TOOL_LABELS = ("crm", "case", "evidence", "reply-draft")


class RuntimeMain(Protocol):
    def __call__(
        self,
        argv: list[str] | None = None,
        *,
        provider_composition: ProviderCompositionRoot | None = None,
        gateway_composition: Any = None,
    ) -> int: ...


def supply_chain_offer(environment: Mapping[str, str]) -> GatewayWorkflowOffer:
    return GatewayWorkflowOffer(
        pin=GatewayPublicationPin(
            offer_id=environment.get("BFF_OPENCLAW_OFFER_ID", "supply-chain-on-demand"),
            kind="workflow",
            code="inventory-supply-chain-on-demand",
            version=2,
            publication_digest=environment.get("SUPPLY_CHAIN_ON_DEMAND_WORKFLOW_DIGEST", ""),
            name="inventory_supply_chain_on_demand",
            aliases=("supply_chain_analysis",),
            labels=("inventory", "forecast", "replenishment"),
        ),
        authority=GatewayAuthorityProfile(
            cid=environment.get("TOOL_GATEWAY_CID", "supply-chain-dev"),
            credential_ref=environment.get("SUPPLY_CHAIN_CREDENTIAL_REF", ""),
            scopes=SUPPLY_CHAIN_SCOPES,
            authorize_path="/internal/supply-chain/v2/openclaw/authorize",
            session_key_prefix=session_key_prefix_for(
                environment.get("BFF_OPENCLAW_AGENT_ID", "main").strip() or "main"
            ),
        ),
    )


def crm_offer(
    environment: Mapping[str, str], config: DeploymentCompositionConfig
) -> GatewayWorkflowOffer:
    if "yeaher.crm" not in {provider.provider_id for provider in config.base_ai_providers}:
        raise ValueError("CRM Tool Gateway requires the approved read-only CRM provider")
    if config.runtime_plugin_policy is None or CRM_PLUGIN_ID not in {
        plugin.plugin_id for plugin in config.runtime_plugin_policy.plugins
    }:
        raise ValueError("CRM Tool Gateway requires the ebizhub.crm-agent plugin pin")
    if config.crm_release is None:
        raise ValueError("CRM Tool Gateway requires the crm_release block")
    digest = environment.get("CRM_ADVISOR_WORKFLOW_DIGEST", "").strip()
    if digest != config.crm_release.advise_workflow.digest:
        raise ValueError(
            "CRM_ADVISOR_WORKFLOW_DIGEST must equal crm_release.advise_workflow.digest"
        )
    return GatewayWorkflowOffer(
        pin=GatewayPublicationPin(
            offer_id=environment.get("BFF_CRM_OPENCLAW_OFFER_ID", CRM_OFFER_ID),
            kind="workflow",
            code=CRM_ADVISE_WORKFLOW,
            version=config.crm_release.advise_workflow.version,
            publication_digest=digest,
            name=CRM_TOOL_NAME,
            aliases=CRM_TOOL_ALIASES,
            labels=CRM_TOOL_LABELS,
        ),
        authority=GatewayAuthorityProfile(
            cid=environment.get("CRM_TOOL_GATEWAY_CID", "crm-advisor"),
            credential_ref=environment.get("CRM_CREDENTIAL_REF", ""),
            scopes=CRM_SCOPES,
            authorize_path="/internal/crm/v2/openclaw/authorize",
            session_key_prefix=session_key_prefix_for(
                environment.get("BFF_CRM_OPENCLAW_AGENT_ID", "crm").strip() or "crm"
            ),
        ),
    )


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
        offers.append(supply_chain_offer(environment))
    if environment.get("CRM_TOOL_GATEWAY_ENABLED", "false").lower() == "true":
        offers.append(crm_offer(environment, config))
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


__all__ = [
    "CRM_OFFER_ID",
    "CRM_SCOPES",
    "CRM_TOOL_ALIASES",
    "CRM_TOOL_LABELS",
    "CRM_TOOL_NAME",
    "SUPPLY_CHAIN_SCOPES",
    "crm_offer",
    "launch",
    "main",
    "supply_chain_offer",
]
