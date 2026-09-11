from __future__ import annotations

from datetime import UTC, datetime

import httpx
import jwt
import pytest
from agent_runtime.connectors.contracts import ConnectorInvocation

from ebiz_deployment.on_demand_provider import OnDemandContextProvider


@pytest.mark.asyncio
async def test_provider_adds_trusted_identity_and_keeps_model_payload_sku_only() -> None:
    secret = "s" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, secret, algorithms=["HS256"], audience="agent-runtime")
        assert claims["tenant_id"] == "tenant-a"
        assert request.url.path == "/internal/supply-chain/v2/on-demand-context"
        assert request.read().decode() == (
            '{"skus":["SKU-1","SKU-2"],"invocation_key":"stable-request"}'
        )
        return httpx.Response(
            200,
            json={
                "schema_version": "supply-chain.on-demand-context.v1",
                "report_run_id": "00000000-0000-4000-8000-000000000001",
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8100",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = OnDemandContextProvider(client=client, jwt_secret=secret)
        response = await provider.invoke(
            ConnectorInvocation(
                tenant_id="tenant-a",
                target="deployment.supply-chain-on-demand-context",
                operation="supply_chain.prepare_on_demand_context",
                capability_version=1,
                payload={"skus": ["SKU-1", "SKU-2"]},
                timeout_seconds=10,
                invocation_key="stable-request",
                trace_id="trace-a",
                effect="PREVIEW",
                deadline_at=datetime.now(UTC),
            )
        )

    assert response.output["schema_version"] == "supply-chain.on-demand-context.v1"
