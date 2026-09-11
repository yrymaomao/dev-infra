"""Deployment-owned trusted context provider for the SKU-only OpenClaw workflow."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import jwt
from agent_runtime.application.provider_composition import (
    ManagedProviderComposition,
    ProviderCompositionRoot,
    merge_plugin_snapshots,
)
from agent_runtime.connectors.contracts import ConnectorInvocation, ConnectorResponse
from agent_runtime.plugins.contracts import (
    PluginPin,
    PluginSnapshot,
    ProviderBinding,
    ProviderKind,
    ProviderRegistration,
)

PROVIDER_ID = "deployment.supply-chain-on-demand-context"
OPERATION = "supply_chain.prepare_on_demand_context"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class OnDemandContextProvider:
    def __init__(self, *, client: httpx.AsyncClient, jwt_secret: str) -> None:
        self._client = client
        self._jwt_secret = jwt_secret

    async def health_check(self) -> bool:
        return True

    async def invoke(self, invocation: ConnectorInvocation) -> ConnectorResponse:
        if invocation.operation != OPERATION or invocation.effect != "PREVIEW":
            raise ValueError("CONNECTOR_OPERATION_INVALID")
        skus = invocation.payload.get("skus")
        if not isinstance(skus, list):
            raise ValueError("CONNECTOR_PAYLOAD_INVALID")
        token = jwt.encode(
            {
                "aud": "agent-runtime",
                "sub": "runtime-on-demand-context",
                "tenant_id": invocation.tenant_id,
                "exp": int(time.time()) + 60,
            },
            self._jwt_secret,
            algorithm="HS256",
        )
        response = await self._client.post(
            "/internal/supply-chain/v2/on-demand-context",
            headers={"Authorization": f"Bearer {token}"},
            json={"skus": skus, "invocation_key": invocation.invocation_key},
        )
        if response.status_code != 200:
            await response.aread()
            raise RuntimeError("ON_DEMAND_CONTEXT_UNAVAILABLE")
        value = response.json()
        if not isinstance(value, dict) or value.get("schema_version") != (
            "supply-chain.on-demand-context.v1"
        ):
            raise RuntimeError("ON_DEMAND_CONTEXT_INVALID")
        return ConnectorResponse(
            output=value,
            source_system="supply-chain-bff",
            external_object_id=invocation.invocation_key,
            captured_at=datetime.now(UTC),
            duration_ms=0,
        )


@dataclass(slots=True)
class _CombinedComposition:
    snapshot: PluginSnapshot
    _base: ManagedProviderComposition
    _client: httpx.AsyncClient
    _closed: bool = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()
        await self._base.aclose()


class OnDemandProviderComposition:
    """Add one exact Deployment connector to the existing Base AI composition."""

    def __init__(
        self,
        *,
        base: ProviderCompositionRoot,
        bff_url: str,
        jwt_secret: str,
        package_digest: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(bff_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or not parsed.netloc
        ):
            raise ValueError("on-demand BFF URL must be loopback HTTP")
        if len(jwt_secret) < 32 or _DIGEST.fullmatch(package_digest) is None:
            raise ValueError("on-demand provider attestation is invalid")
        self._base = base
        self._bff_url = bff_url.rstrip("/")
        self._jwt_secret = jwt_secret
        self._package_digest = package_digest
        self._client = client

    async def start(self) -> _CombinedComposition:
        base = await self._base.start()
        client = self._client or httpx.AsyncClient(
            base_url=self._bff_url,
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        )
        try:
            pin = PluginPin(
                plugin_id="deployment.supply-chain-on-demand",
                version="0.1.4",
                package_name="ebiz-deployment-composition",
                entry_point="ebiz_deployment.on_demand_provider:OnDemandContextProvider",
                package_digest=self._package_digest,
            )
            provider = OnDemandContextProvider(client=client, jwt_secret=self._jwt_secret)
            deployment = PluginSnapshot(
                plugins=(pin,),
                providers=(
                    ProviderBinding(
                        plugin=pin,
                        registration=ProviderRegistration(
                            provider_id=PROVIDER_ID,
                            kind=ProviderKind.CONNECTOR,
                            provider=provider,
                            permissions=frozenset({"supply_chain.preview"}),
                            network_targets=frozenset({parsed_host(self._bff_url)}),
                        ),
                    ),
                ),
            )
            return _CombinedComposition(
                snapshot=merge_plugin_snapshots(base.snapshot, deployment),
                _base=base,
                _client=client,
            )
        except BaseException:
            await client.aclose()
            await base.aclose()
            raise


def parsed_host(origin: str) -> str:
    host = urlsplit(origin).hostname
    if host is None:
        raise ValueError("on-demand BFF host is unavailable")
    return host


__all__ = ["OPERATION", "PROVIDER_ID", "OnDemandContextProvider", "OnDemandProviderComposition"]
