from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agent_runtime.application.tool_gateway_authorization import ToolGatewayPolicyRejected
from agent_runtime.application.tool_gateway_operations import GatewayOperationConflict
from ebiz_runtime_contracts.tool_gateway import ToolGatewayIdentity
from ebiz_runtime_contracts.tool_gateway_catalog import ToolOfferReference
from ebiz_runtime_contracts.tool_gateway_policy import (
    PolicyTarget,
    ToolGatewayPolicyReply,
    ToolGatewayPolicyRequest,
)

from ebiz_deployment.supply_chain_credential_bindings import CredentialBindings
from ebiz_deployment.tool_gateway import (
    BffToolGatewayPolicyPort,
    DynamicGatewayContext,
    GatewayAuthorityProfile,
    GatewayPolicyRoute,
)

SUPPLY_CHAIN_KEY = "agent:main:openclaw:" + "1" * 48
CRM_KEY = "agent:crm:openclaw:" + "2" * 48


def _identity(session_key: str = SUPPLY_CHAIN_KEY) -> ToolGatewayIdentity:
    now = datetime.now(UTC)
    return ToolGatewayIdentity(
        tenant_id="tenant-a",
        principal_id="principal-a",
        session_key=session_key,
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
@pytest.mark.parametrize("bff_digest,allowed", [("a" * 64, True), ("b" * 64, False)])
async def test_gateway_rejects_bff_binding_digest_drift_before_invocation(
    bff_digest: str, allowed: bool
) -> None:
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8100",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "binding_active": True,
                    "policy_revision": "1",
                    "allowed_offer_ids": ["supply-chain-on-demand"],
                    "credential_binding_digest": bff_digest,
                },
            )
        ),
    ) as client:
        port = BffToolGatewayPolicyPort(
            client=client,
            connector_credential="c" * 32,
            expected_supply_chain_binding_digest="a" * 64,
        )
        request = ToolGatewayPolicyRequest(
            identity=_identity(),
            action="invoke",
            candidates=(PolicyTarget(kind="offer", target_id="supply-chain-on-demand"),),
        )
        if allowed:
            assert (await port.authorize(request)).binding_active is True
        else:
            with pytest.raises(ToolGatewayPolicyRejected):
                await port.authorize(request)


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
            session_key=SUPPLY_CHAIN_KEY,
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
    assert context.session_key == SUPPLY_CHAIN_KEY
    assert context.auth.scopes == frozenset(
        {
            "workflow:start",
            "runtime:admission",
            "inventory.read",
            "sales_profit.read",
            "supply_chain.preview",
        }
    )


def test_supply_chain_context_selects_only_signed_users_bound_reference() -> None:
    reference = ToolOfferReference(
        generation_id="generation-a",
        offer_id="supply-chain-on-demand",
        version=1,
        publication_digest="a" * 64,
        input_schema_digest="b" * 64,
        output_schema_digest="c" * 64,
    )
    resolver = DynamicGatewayContext(
        connector_id="openclaw",
        tenant_id="tenant-a",
        profiles=(
            (
                reference,
                GatewayAuthorityProfile(
                    cid="supply-chain-dev",
                    credential_ref="opaque:legacy",
                    scopes=frozenset({"workflow:start", "runtime:admission"}),
                ),
            ),
        ),
        credential_bindings=CredentialBindings(
            digest="a" * 64,
            _by_owner={("tenant-a", "alice"): "key:alice", ("tenant-a", "bob"): "key:bob"},
        ),
        user_bound_offer_ids=frozenset({reference.offer_id}),
    )

    def decision(uid: str) -> ToolGatewayPolicyReply:
        return ToolGatewayPolicyReply(
            tenant_id="tenant-a",
            principal_id=uid,
            session_key=SUPPLY_CHAIN_KEY,
            binding_active=True,
            action="invoke",
            policy_revision="1",
            allowed_targets=(PolicyTarget(kind="offer", target_id=reference.offer_id),),
        )

    alice = resolver.resolve(decision("alice"), reference)
    bob = resolver.resolve(decision("bob"), reference)
    assert alice.auth.credential_ref == "key:alice"
    assert bob.auth.credential_ref == "key:bob"
    assert alice.auth.cid == bob.auth.cid == "tenant-a"
    with pytest.raises(GatewayOperationConflict):
        resolver.resolve(decision("charlie"), reference)


@pytest.mark.asyncio
async def test_policy_port_routes_each_agent_session_to_its_own_reception_profile() -> None:
    from agent_runtime.application.tool_gateway_authorization import ToolGatewayPolicyRejected

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        offer = "crm-case-advice" if "/crm/" in request.url.path else "supply-chain-on-demand"
        return httpx.Response(
            200,
            json={"binding_active": True, "policy_revision": "1", "allowed_offer_ids": [offer]},
        )

    routes = (
        GatewayPolicyRoute("agent:main:openclaw:", "/internal/supply-chain/v2/openclaw/authorize"),
        GatewayPolicyRoute("agent:crm:openclaw:", "/internal/crm/v2/openclaw/authorize"),
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8100", transport=httpx.MockTransport(handler)
    ) as client:
        port = BffToolGatewayPolicyPort(client=client, connector_credential="c" * 32, routes=routes)
        candidates = (
            PolicyTarget(kind="offer", target_id="supply-chain-on-demand"),
            PolicyTarget(kind="offer", target_id="crm-case-advice"),
        )
        crm = await port.authorize(
            ToolGatewayPolicyRequest(
                identity=_identity(CRM_KEY), action="discover", candidates=candidates
            )
        )
        supply_chain = await port.authorize(
            ToolGatewayPolicyRequest(
                identity=_identity(SUPPLY_CHAIN_KEY), action="discover", candidates=candidates
            )
        )
        with pytest.raises(ToolGatewayPolicyRejected):
            await port.authorize(
                ToolGatewayPolicyRequest(
                    identity=_identity("agent:other:openclaw:" + "3" * 48),
                    action="discover",
                    candidates=candidates,
                )
            )
    assert seen == [
        "/internal/crm/v2/openclaw/authorize",
        "/internal/supply-chain/v2/openclaw/authorize",
    ]
    assert crm.allowed_targets == (PolicyTarget(kind="offer", target_id="crm-case-advice"),)
    assert supply_chain.allowed_targets == (
        PolicyTarget(kind="offer", target_id="supply-chain-on-demand"),
    )


def test_policy_routes_reject_malformed_prefixes_and_paths() -> None:
    with pytest.raises(ValueError):
        GatewayPolicyRoute("session-", "/internal/crm/v2/openclaw/authorize")
    with pytest.raises(ValueError):
        GatewayPolicyRoute("agent:crm:openclaw:", "/api/crm/v2/openclaw/authorize")
