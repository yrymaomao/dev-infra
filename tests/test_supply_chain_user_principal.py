from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from ebiz_deployment.crm_reception.session_api import (
    SessionInvalid,
    SessionPrincipal,
    SessionUnavailable,
)
from ebiz_deployment.supply_chain_bff.app import BffContainer, Principal, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner
from ebiz_deployment.supply_chain_bff.reception import supply_chain_owner
from ebiz_deployment.supply_chain_credential_bindings import CredentialBindings


class FakeSessionClient:
    async def resolve(self, token: str) -> SessionPrincipal:
        if token == "expired":
            raise SessionInvalid(401)
        if token == "unavailable":
            raise SessionUnavailable()
        if token == "unbound":
            return SessionPrincipal(tenant_id="tenant-a", actor_id="charlie", roles=("USER",))
        if token == "wrong-tenant":
            return SessionPrincipal(tenant_id="tenant-b", actor_id="alice", roles=("USER",))
        if token != "erp-session":
            raise AssertionError("unexpected bearer")
        return SessionPrincipal(tenant_id="tenant-a", actor_id="alice", roles=("USER",))


def test_user_bound_chat_owner_accepts_distinct_users_in_configured_tenant() -> None:
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:legacy",
        user_bound_credentials_enabled=True,
        legacy_batches_enabled=False,
        openclaw_enabled=True,
        openclaw_tenant_id="tenant-a",
    )
    owner = supply_chain_owner(settings)
    assert owner(Principal("tenant-a", "alice")).principal_id == "alice"
    assert owner(Principal("tenant-a", "bob")).principal_id == "bob"
    with pytest.raises(HTTPException) as denied:
        owner(Principal("tenant-b", "alice"))
    assert denied.value.status_code == 403


def test_user_bound_bff_refuses_start_without_binding_map() -> None:
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:legacy",
        user_bound_credentials_enabled=True,
        legacy_batches_enabled=False,
        crm_service_url="https://crm.example.test",
    )
    with pytest.raises(ValueError, match="credential bindings"):
        create_app(
            BffContainer(
                settings=settings,
                repository=object(),  # type: ignore[arg-type]
                runtime=object(),  # type: ignore[arg-type]
                coordinator=object(),  # type: ignore[arg-type]
                cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=1)),
            )
        )


@pytest.mark.anyio
async def test_user_bound_public_route_resolves_erp_identity_and_rejects_invalid_session() -> None:
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:legacy",
        user_bound_credentials_enabled=True,
        legacy_batches_enabled=False,
        credential_bindings=CredentialBindings(
            digest="a" * 64, _by_owner={("tenant-a", "alice"): "key:alice"}
        ),
        crm_service_url="https://crm.example.test",
        crm_openclaw_enabled=True,
        crm_openclaw_tenant_id="tenant-a",
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=1)),
            crm_session_client=FakeSessionClient(),
            level2_repository=SimpleNamespace(_factory=object(), _payload_store=object()),  # type: ignore[arg-type]
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff.test"
    ) as client:
        good = await client.get(
            "/api/supply-chain/v2/runtime-profile",
            headers={"Authorization": "Bearer erp-session"},
        )
        expired = await client.get(
            "/api/supply-chain/v2/runtime-profile",
            headers={"Authorization": "Bearer expired"},
        )
        unavailable = await client.get(
            "/api/supply-chain/v2/runtime-profile",
            headers={"Authorization": "Bearer unavailable"},
        )
        unbound = await client.get(
            "/api/supply-chain/v2/runtime-profile",
            headers={"Authorization": "Bearer unbound"},
        )
        wrong_tenant = await client.get(
            "/api/supply-chain/v2/runtime-profile",
            headers={"Authorization": "Bearer wrong-tenant"},
        )
        crm_token = await client.get(
            "/api/crm/v2/openclaw/conversations/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": "Bearer erp-session"},
        )
    assert good.status_code == 200
    assert expired.status_code == 401
    assert unavailable.status_code == 503
    assert unbound.status_code == 403
    assert wrong_tenant.status_code == 403
    assert crm_token.status_code == 401
