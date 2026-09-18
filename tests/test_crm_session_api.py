"""R1-09: workbench ``_token_`` -> crm-service session-principal -> BFF JWT (decision D-1)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from crm_fixtures import TENANT, advised_result
from reception_fixtures import bff_settings

from ebiz_deployment.crm_reception.session_api import (
    CrmSessionPrincipalClient,
    SessionInvalid,
    SessionPrincipal,
    SessionUnavailable,
)
from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner

JWT_SECRET = "j" * 32
SESSION = "/api/crm/v2/openclaw/session"
CONVERSATIONS = "/api/crm/v2/openclaw/conversations"


class FakeSessionClient:
    def __init__(self, outcome: SessionPrincipal | Exception) -> None:
        self.outcome = outcome
        self.tokens: list[str] = []

    async def resolve(self, token: str) -> SessionPrincipal:
        self.tokens.append(token)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def crm_app(client: FakeSessionClient, *, level2_repository: object | None = None):
    settings = bff_settings(
        openclaw_enabled=True,
        openclaw_connector_credential="c" * 32,
        tool_gateway_jwt_key="g" * 32,
        crm_openclaw_enabled=True,
        crm_openclaw_tenant_id=TENANT,
        crm_openclaw_ingress_credential="s" * 32,
    )
    return create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            level2_repository=level2_repository,  # type: ignore[arg-type]
            crm_session_client=client,
        )
    )


@pytest.mark.asyncio
async def test_human_session_becomes_a_120s_bff_jwt_in_the_conversation_shape() -> None:
    client = FakeSessionClient(SessionPrincipal(TENANT, "agent-7", ("CRM_AGENT",)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=crm_app(client)), base_url="http://bff.test"
    ) as http:
        response = await http.post(SESSION, headers={"_token_": "erp-session-token"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert client.tokens == ["erp-session-token"]
    claims = jwt.decode(body["jwt"], JWT_SECRET, algorithms=["HS256"], audience="agent-runtime")
    assert claims["sub"] == "agent-7"
    assert claims["tenant_id"] == TENANT
    assert claims["session_key"].startswith("workbench:crm:")
    assert claims["exp"] - claims["iat"] == 120
    assert len(claims["jti"]) == 32
    assert body["expiresAt"] == claims["exp"] * 1000
    assert body["principal"] == {"tenant_id": TENANT, "actor_id": "agent-7", "roles": ["CRM_AGENT"]}
    assert "erp-session-token" not in response.text


@pytest.mark.asyncio
async def test_cookie_token_is_accepted_when_no_header_is_sent() -> None:
    client = FakeSessionClient(SessionPrincipal(TENANT, "agent-7", ()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=crm_app(client)),
        base_url="http://bff.test",
        cookies={"_token_": "cookie-session"},
    ) as http:
        response = await http.post(SESSION)
    assert response.status_code == 200
    assert client.tokens == ["cookie-session"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (SessionInvalid(401), 401),
        (SessionInvalid(403), 403),  # a machine session is not a workbench user
        (SessionUnavailable(), 503),
    ],
)
async def test_crm_service_refusals_pass_through_without_detail(outcome, status) -> None:
    client = FakeSessionClient(outcome)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=crm_app(client)), base_url="http://bff.test"
    ) as http:
        response = await http.post(SESSION, headers={"_token_": "whatever"})
    assert response.status_code == status
    if status != 503:
        assert response.json()["error_code"] in {"BFF_HTTP_401", "BFF_HTTP_403"} or (
            "session invalid" in response.text
        )
    assert "machine" not in response.text.lower()


@pytest.mark.asyncio
async def test_missing_token_is_401_without_calling_crm_service() -> None:
    client = FakeSessionClient(SessionPrincipal(TENANT, "agent-7", ()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=crm_app(client)), base_url="http://bff.test"
    ) as http:
        response = await http.post(SESSION)
    assert response.status_code == 401
    assert client.tokens == []


@pytest.mark.asyncio
async def test_cross_tenant_session_is_refused_before_any_conversation_or_result() -> None:
    # crm-service says the session belongs to another tenant: no JWT is minted, so no
    # conversation route (and no stored result behind it) can be addressed.
    client = FakeSessionClient(SessionPrincipal("tenant-b", "agent-7", ("CRM_AGENT",)))
    repository = SimpleNamespace(
        _factory=object(), _payload_store=object(), snapshot=AsyncMock(return_value={})
    )
    app = crm_app(client, level2_repository=repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff.test"
    ) as http:
        response = await http.post(SESSION, headers={"_token_": "other-tenant-session"})
        assert response.status_code == 403
        assert "jwt" not in response.text
        # A JWT for another tenant, however obtained, is refused by the owner policy too.
        foreign = jwt.encode(
            {
                "aud": "agent-runtime",
                "sub": "agent-7",
                "tenant_id": "tenant-b",
                "exp": 4102444800,
            },
            JWT_SECRET,
            algorithm="HS256",
        )
        blocked = await http.post(CONVERSATIONS, headers={"Authorization": f"Bearer {foreign}"})
    assert blocked.status_code == 403
    repository.snapshot.assert_not_awaited()
    assert (
        "advice" not in blocked.text and advised_result()["payload"]["case_id"] not in blocked.text
    )


@pytest.mark.asyncio
async def test_session_route_is_absent_when_the_crm_profile_is_disabled() -> None:
    settings = bff_settings(openclaw_enabled=True, openclaw_connector_credential="c" * 32)
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff.test"
    ) as http:
        response = await http.post(SESSION, headers={"_token_": "x"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_real_client_sends_the_token_header_and_maps_crm_service_answers() -> None:
    seen: list[httpx.Request] = []

    def crm_service(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        token = request.headers.get("_token_")
        if token == "human":
            return httpx.Response(
                200, json={"tenantId": TENANT, "actorId": "agent-7", "roles": ["CRM_AGENT"]}
            )
        if token == "machine":
            return httpx.Response(403, json={"error": "machine session"})
        if token == "broken":
            return httpx.Response(200, json={"tenantId": TENANT})
        return httpx.Response(401)

    client = CrmSessionPrincipalClient(
        "http://127.0.0.1:9090", transport=httpx.MockTransport(crm_service)
    )
    principal = await client.resolve("human")
    assert principal == SessionPrincipal(TENANT, "agent-7", ("CRM_AGENT",))
    assert seen[0].url.path == "/ai/read/v1/crm/session-principal"
    assert seen[0].method == "GET"
    with pytest.raises(SessionInvalid) as denied:
        await client.resolve("machine")
    assert denied.value.status == 403
    with pytest.raises(SessionInvalid) as expired:
        await client.resolve("expired")
    assert expired.value.status == 401
    with pytest.raises(SessionUnavailable):
        await client.resolve("broken")
    with pytest.raises(ValueError):
        CrmSessionPrincipalClient("http://crm.internal:8080")
