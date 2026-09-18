"""R1-06: each reception profile authorizes only its own offers for its own run bindings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from crm_fixtures import TENANT
from fastapi import FastAPI
from reception_fixtures import bff_settings, crm

from ebiz_deployment.openclaw_reception.connector_api import RunTokenIssuer, connector_router
from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner

CONNECTOR = "c" * 32
GATEWAY_KEY = "g" * 32
SUPPLY_CHAIN_KEY = "agent:main:openclaw:" + "1" * 48
CRM_KEY = "agent:crm:openclaw:" + "2" * 48
CANDIDATES = ["supply-chain-on-demand", "crm-case-advice", "crm-send", "other"]


class FakeBindings:
    """Both profiles share one table; the fake records every key that was asked about."""

    def __init__(self) -> None:
        self.active_keys = {SUPPLY_CHAIN_KEY, CRM_KEY}
        self.asked: list[str] = []
        self.exchanged: list[dict[str, object]] = []
        self.ended: list[str] = []
        self._factory = object()
        self._payload_store = object()

    async def exchange_openclaw_run(self, **kwargs: object) -> dict[str, str]:
        self.exchanged.append(kwargs)
        prefix = f"agent:{kwargs['agent_id']}:openclaw:"
        return {
            "tenantId": str(kwargs["tenant_id"]),
            "principalId": str(kwargs["principal_id"]),
            "sessionId": "00000000-0000-4000-8000-000000000009",
            "sessionKey": prefix + "9" * 48,
            "runId": str(kwargs["run_id"]),
        }

    async def end_openclaw_run(self, *, run_id: str, now: datetime) -> bool:
        self.ended.append(run_id)
        return True

    async def authorize_openclaw_run(
        self, *, tenant_id: str, principal_id: str, session_key: str, now: datetime
    ) -> tuple[bool, str]:
        self.asked.append(session_key)
        return session_key in self.active_keys, "5"


class FakeConversations:
    def __init__(self, tid: UUID) -> None:
        self.tid = tid

    async def credential_context(self, selector: str, run_id: str) -> tuple[str, str, str, UUID]:
        assert selector == f"turn:{self.tid}" and run_id == str(self.tid)
        return TENANT, "agent-7", "model-session-1", self.tid


def both_profiles_app(bindings: FakeBindings):
    settings = bff_settings(
        level2_enabled=True,
        openclaw_enabled=True,
        openclaw_tenant_id=TENANT,
        openclaw_connector_credential=CONNECTOR,
        tool_gateway_jwt_key=GATEWAY_KEY,
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
            level2_repository=bindings,  # type: ignore[arg-type]
            session_factory=object(),
            payload_store=object(),
            crm_session_client=object(),  # type: ignore[arg-type]
            run_bindings=bindings,
        )
    )


def policy(session_key: str, action: str = "invoke") -> dict[str, object]:
    return {
        "tenant_id": TENANT,
        "principal_id": "agent-7",
        "session_key": session_key,
        "action": action,
        "candidate_offer_ids": CANDIDATES,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "own_key", "own_offer"),
    [
        (
            "/internal/supply-chain/v2/openclaw/authorize",
            SUPPLY_CHAIN_KEY,
            "supply-chain-on-demand",
        ),
        ("/internal/crm/v2/openclaw/authorize", CRM_KEY, "crm-case-advice"),
    ],
)
async def test_each_profile_authorizes_only_its_own_offer(path, own_key, own_offer) -> None:
    bindings = FakeBindings()
    headers = {"Authorization": f"Bearer {CONNECTOR}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=both_profiles_app(bindings)),
        base_url="http://bff.test",
        headers=headers,
    ) as client:
        own = await client.post(path, json=policy(own_key))
        assert own.status_code == 200, own.text
        assert own.json() == {
            "binding_active": True,
            "policy_revision": "5",
            "allowed_offer_ids": [own_offer],
        }
        # The other profile's key: refused before the binding table is even consulted.
        other_key = CRM_KEY if own_key == SUPPLY_CHAIN_KEY else SUPPLY_CHAIN_KEY
        asked_before = list(bindings.asked)
        foreign = await client.post(path, json=policy(other_key))
        assert foreign.json() == {
            "binding_active": False,
            "policy_revision": "0",
            "allowed_offer_ids": [],
        }
        assert bindings.asked == asked_before
        # An inactive binding of the right shape: no offers either.
        bindings.active_keys.clear()
        inactive = await client.post(path, json=policy(own_key))
        assert inactive.json()["binding_active"] is False
        assert inactive.json()["allowed_offer_ids"] == []
        # Operation actions carry no offers; the reply only says whether the run is bound.
        bindings.active_keys.add(own_key)
        status = await client.post(
            path, json={**policy(own_key, "status"), "candidate_offer_ids": []}
        )
        assert status.json() == {
            "binding_active": True,
            "policy_revision": "5",
            "allowed_offer_ids": [],
        }


@pytest.mark.asyncio
async def test_connector_credential_guards_every_profile_route() -> None:
    bindings = FakeBindings()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=both_profiles_app(bindings)), base_url="http://bff.test"
    ) as client:
        for path in (
            "/internal/supply-chain/v2/openclaw/authorize",
            "/internal/crm/v2/openclaw/authorize",
        ):
            assert (await client.post(path, json=policy(CRM_KEY))).status_code == 403
        for path in (
            "/api/crm/v2/openclaw/credentials",
            "/api/supply-chain/v2/openclaw/credentials",
        ):
            wrong = await client.post(
                path,
                json={"selector": "x", "runId": "r"},
                headers={"Authorization": "Bearer " + "x" * 32},
            )
            assert wrong.status_code == 403
    assert bindings.asked == []


@pytest.mark.asyncio
async def test_crm_credentials_bind_turn_selectors_only_and_mint_the_crm_prefixed_key() -> None:
    bindings = FakeBindings()
    tid = uuid4()
    app = FastAPI()
    app.include_router(
        connector_router(
            profile=crm(bff_settings(crm_openclaw_tenant_id=TENANT)),
            bindings=lambda: bindings,
            conversations=FakeConversations(tid),  # type: ignore[arg-type]
            connector=lambda: None,
            tokens=RunTokenIssuer(key=GATEWAY_KEY, issuer="bff", audience="ebizhub-tool-gateway"),
            static_identity=None,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff.test"
    ) as client:
        # No deployment-constant identity exists for CRM: a static selector never binds.
        static = await client.post(
            "/api/crm/v2/openclaw/credentials",
            json={"selector": "supply-chain-dev", "runId": "run-a"},
        )
        assert static.status_code == 403
        assert bindings.exchanged == []
        bound = await client.post(
            "/api/crm/v2/openclaw/credentials",
            json={"selector": f"turn:{tid}", "runId": str(tid)},
        )
        assert bound.status_code == 200, bound.text
        exchanged = bindings.exchanged[-1]
        assert exchanged["agent_id"] == "crm"
        assert exchanged["tenant_id"] == TENANT and exchanged["principal_id"] == "agent-7"
        assert exchanged["model_session_id"] == "model-session-1" and exchanged["turn_id"] == tid
        claims = jwt.decode(
            bound.json()["jwt"], GATEWAY_KEY, algorithms=["HS256"], audience="ebizhub-tool-gateway"
        )
        assert claims["session_key"].startswith("agent:crm:openclaw:")
        assert claims["sub"] == "agent-7" and claims["tenant_id"] == TENANT
        assert claims["exp"] - claims["iat"] == 120


@pytest.mark.asyncio
async def test_supply_chain_static_selector_still_binds_the_deployment_constant_identity() -> None:
    bindings = FakeBindings()
    headers = {"Authorization": f"Bearer {CONNECTOR}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=both_profiles_app(bindings)),
        base_url="http://bff.test",
        headers=headers,
    ) as client:
        credential = await client.post(
            "/api/supply-chain/v2/openclaw/credentials",
            json={"selector": "supply-chain-dev", "runId": "run-a"},
        )
        assert credential.status_code == 200, credential.text
        claims = jwt.decode(
            credential.json()["jwt"],
            GATEWAY_KEY,
            algorithms=["HS256"],
            audience="ebizhub-tool-gateway",
        )
        assert claims["session_key"].startswith("agent:main:openclaw:")
        assert claims["tenant_id"] == TENANT
        assert bindings.exchanged[-1]["agent_id"] == "main"
        assert bindings.exchanged[-1]["principal_id"] == "openclaw-supply-chain"
        ended = await client.post("/api/supply-chain/v2/openclaw/runs/end", json={"runId": "run-a"})
        assert ended.json() == {"ended": True}
        crm_ended = await client.post("/api/crm/v2/openclaw/runs/end", json={"runId": "run-b"})
        assert crm_ended.json() == {"ended": True}
    assert bindings.ended == ["run-a", "run-b"]
    assert datetime.now(UTC).tzinfo is UTC
