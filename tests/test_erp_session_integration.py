"""Both BFF entrypoints consume the same verified ERP session resolver."""

import jwt
import pytest
from test_erp_session import resolver, state, token


@pytest.mark.asyncio
async def test_crm_and_supply_chain_use_one_erp_resolver_without_crm_service() -> None:
    from datetime import timedelta

    import httpx

    from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
    from ebiz_deployment.supply_chain_bff.config import BffSettings
    from ebiz_deployment.supply_chain_bff.cursor import CursorSigner
    from ebiz_deployment.user_mcp_credentials import UserMcpRefCodec

    presented = token()
    client, store = resolver({"_Cache_Login_User_Data_alice": state(presented)})
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8013",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:legacy",
        user_bound_credentials_enabled=True,
        user_mcp_ref_codec=UserMcpRefCodec(b"s" * 32),
        legacy_batches_enabled=False,
        openclaw_tenant_id="222260",
        crm_openclaw_enabled=True,
        crm_openclaw_tenant_id="222260",
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),
            runtime=object(),
            coordinator=object(),
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=1)),
            session_principal_client=client,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff.test"
    ) as http:
        crm = await http.post("/api/crm/v2/openclaw/session", headers={"_token_": presented})
        supply = await http.get(
            "/api/supply-chain/v2/runtime-profile", headers={"Authorization": f"Bearer {presented}"}
        )
        assert crm.status_code == supply.status_code == 200
        assert crm.json()["principal"] == {
            "tenant_id": "222260",
            "actor_id": "alice",
            "roles": ["CRM_AGENT", "USER"],
        }
        internal = jwt.decode(
            crm.json()["jwt"], "j" * 32, algorithms=["HS256"], audience="agent-runtime"
        )
        assert internal["exp"] - internal["iat"] == 120
        store.values.clear()
        assert (
            await http.get(
                "/api/supply-chain/v2/runtime-profile",
                headers={"Authorization": f"Bearer {presented}"},
            )
        ).status_code == 401
