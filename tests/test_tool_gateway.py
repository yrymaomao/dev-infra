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

from ebiz_deployment.tool_gateway import (
    BffToolGatewayPolicyPort,
    DynamicGatewayContext,
    GatewayAuthorityProfile,
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "result", "cancel"])
async def test_policy_port_preserves_owned_operation_target_for_active_run(action: str) -> None:
    target = PolicyTarget(kind="operation", target_id="8b3dd260-613b-4a76-abee-c66c544e49c0")

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        assert '"candidate_offer_ids":[]' in body
        return httpx.Response(
            200,
            json={
                "binding_active": True,
                "policy_revision": "7",
                "allowed_offer_ids": [],
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
                action=action,
                candidates=(target,),
            )
        )

    assert reply.allowed_targets == (target,)


@pytest.mark.asyncio
async def test_policy_port_denies_operation_target_for_inactive_run() -> None:
    target = PolicyTarget(kind="operation", target_id="8b3dd260-613b-4a76-abee-c66c544e49c0")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8100",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "binding_active": False,
                    "policy_revision": "8",
                    "allowed_offer_ids": [],
                },
            )
        ),
    ) as client:
        reply = await BffToolGatewayPolicyPort(
            client=client,
            connector_credential="c" * 32,
        ).authorize(
            ToolGatewayPolicyRequest(
                identity=_identity(),
                action="status",
                candidates=(target,),
            )
        )

    assert reply.allowed_targets == ()


def test_dynamic_context_uses_authorized_identity_not_tool_arguments() -> None:
    reference = ToolOfferReference(
        generation_id="generation-a",
        offer_id="supply-chain-on-demand",
        version=1,
        publication_digest="a" * 64,
        input_schema_digest="b" * 64,
        output_schema_digest="c" * 64,
    )
    context = DynamicGatewayContext(
        connector_id="openclaw",
        tenant_id="tenant-a",
        profiles=(
            (
                reference,
                GatewayAuthorityProfile(
                    cid="supply-chain-dev",
                    credential_ref="opaque:dev-erp-mcp",
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
            ),
        ),
    ).resolve(
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
    assert context.auth.credential_ref == "opaque:dev-erp-mcp"
    assert context.session_key == "session-a"
    assert context.auth.scopes == frozenset(
        {
            "workflow:start",
            "runtime:admission",
            "inventory.read",
            "sales_profit.read",
            "supply_chain.preview",
        }
    )
