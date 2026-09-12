"""Production launcher that injects deployment-owned providers into Runtime."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from agent_runtime.application.provider_composition import ProviderCompositionRoot
from agent_runtime.cli import api as runtime_api

from .composition import build_provider_composition
from .config import load_deployment_config
from .on_demand_provider import OnDemandProviderComposition
from .tool_gateway import SupplyChainToolGatewayComposition


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
    if environment.get("SUPPLY_CHAIN_TOOL_GATEWAY_ENABLED", "false").lower() == "true":
        gateway_composition = SupplyChainToolGatewayComposition(
            tenant_id=environment.get("BFF_OPENCLAW_TENANT_ID", "tenant-local-dev"),
            workflow_digest=environment.get("SUPPLY_CHAIN_ON_DEMAND_WORKFLOW_DIGEST", ""),
            bff_url=environment.get("SUPPLY_CHAIN_BFF_INTERNAL_URL", ""),
            connector_credential=environment.get("BFF_OPENCLAW_CONNECTOR_CREDENTIAL", ""),
            jwt_key=environment.get("TOOL_GATEWAY_JWT_KEY", ""),
            jwt_issuer=environment.get("TOOL_GATEWAY_JWT_ISSUER", "ebizhub-supply-chain-bff"),
            jwt_audience=environment.get("TOOL_GATEWAY_JWT_AUDIENCE", "ebizhub-tool-gateway"),
            cid=environment.get("TOOL_GATEWAY_CID", "supply-chain-dev"),
            credential_ref=environment.get("SUPPLY_CHAIN_CREDENTIAL_REF", ""),
            generation_id=environment.get("TOOL_GATEWAY_GENERATION_ID", "supply-chain-v2-dev-1"),
            catalog_revision=environment.get(
                "TOOL_GATEWAY_CATALOG_REVISION", "supply-chain-v2-dev-1"
            ),
            offer_id=environment.get("BFF_OPENCLAW_OFFER_ID", "supply-chain-on-demand"),
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
