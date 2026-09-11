from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from ebiz_runtime_contracts.tool_gateway import ToolGatewayIdentity
from ebiz_runtime_contracts.tool_gateway_catalog import ToolOfferReference
from ebiz_runtime_contracts.tool_gateway_policy import (
    PolicyTarget,
    ToolGatewayPolicyReply,
    ToolGatewayPolicyRequest,
)

from ebiz_deployment.tool_gateway import BffToolGatewayPolicyPort, DynamicGatewayContext


def _identity() -> ToolGatewayIdentity:
    now = datetime.now(UTC)
    return ToolGatewayIdentity(
        tenant_id="tenant-a",
        principal_id="principal-a",
        session_key="session-a",
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
    )


@pytest.mark.asyncio
async def test_policy_port_preserves_current_binding_and_exact_offer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer " + ("c" * 32)
        return httpx.Response(
            200,
            json={
                "binding_active": True,
                "policy_revision": "7",
                "allowed_offer_ids": ["supply-chain-on-demand"],
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8100",
        transport=httpx.MockTransport(handler),
    ) as client:
        reply = await BffToolGatewayPolicyPort(
            client=client,
            connector_credential="c" * 32,
        ).authorize(
            ToolGatewayPolicyRequest(
                identity=_identity(),
                action="invoke",
                candidates=(PolicyTarget(kind="offer", target_id="supply-chain-on-demand"),),
            )
        )

    assert reply.binding_active is True
    assert reply.policy_revision == "7"
    assert reply.allowed_targets == (
        PolicyTarget(kind="offer", target_id="supply-chain-on-demand"),
    )


def test_dynamic_context_uses_authorized_identity_not_tool_arguments() -> None:
    reference = ToolOfferReference(
        generation_id="generation-a",
        offer_id="supply-chain-on-demand",
        version=1,
        publication_digest="a" * 64,
        input_schema_digest="b" * 64,
        output_schema_digest="c" * 64,
    )
    context = DynamicGatewayContext(connector_id="openclaw", cid="supply-chain-dev").resolve(
        ToolGatewayPolicyReply(
            tenant_id="tenant-a",
            principal_id="principal-a",
            session_key="session-a",
            binding_active=True,
            action="invoke",
            policy_revision="1",
            allowed_targets=(PolicyTarget(kind="offer", target_id=reference.offer_id),),
        ),
        reference,
    )
    assert context.auth.tenant_id == "tenant-a"
    assert context.auth.cid == "supply-chain-dev"
    assert context.session_key == "session-a"
    assert context.auth.scopes == frozenset(
        {"workflow:start", "runtime:admission", "supply_chain.preview"}
    )
