"""The browser-facing turn proxy binds tenant and keeps connector credentials server-side."""

from __future__ import annotations

import time
from datetime import timedelta

import httpx
import jwt
import pytest

from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner


def _app(transport: httpx.AsyncBaseTransport):
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        openclaw_enabled=True,
        openclaw_tenant_id="tenant-a",
        openclaw_ingress_credential="s" * 32,
    )
    return create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            openclaw_http_transport=transport,
        )
    )


def _token(tenant: str) -> str:
    return jwt.encode(
        {
            "aud": "agent-runtime",
            "sub": "browser-user",
            "tenant_id": tenant,
            "exp": int(time.time()) + 60,
        },
        "j" * 32,
        algorithm="HS256",
    )


@pytest.mark.anyio
async def test_turn_proxies_original_question_and_only_visible_reply() -> None:
    captured: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "completed": True,
                "runId": "run-1",
                "reply": "供应链管理是协调采购、生产、库存和交付。",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(httpx.MockTransport(upstream))),
        base_url="http://bff.test",
    ) as client:
        response = await client.post(
            "/api/supply-chain/v2/openclaw/turn",
            headers={"Authorization": f"Bearer {_token('tenant-a')}"},
            json={
                "prompt": "什么是供应链管理",
                "client_request_id": "a90ce377-e246-438c-b973-fe6a0fe13f83",
            },
        )
    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "reply": "供应链管理是协调采购、生产、库存和交付。",
    }
    assert len(captured) == 1
    assert captured[0].headers["Authorization"] == "Bearer " + "s" * 32
    assert captured[0].url.path == "/ebiz/tool-gateway/ingress"
    assert captured[0].read().decode("utf-8").find("什么是供应链管理") >= 0
    assert "selector" not in response.text
    assert "Bearer" not in response.text


@pytest.mark.anyio
async def test_turn_rejects_other_tenant_without_contacting_openclaw() -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(httpx.MockTransport(upstream))),
        base_url="http://bff.test",
    ) as client:
        response = await client.post(
            "/api/supply-chain/v2/openclaw/turn",
            headers={"Authorization": f"Bearer {_token('tenant-b')}"},
            json={
                "prompt": "什么是供应链管理",
                "client_request_id": "a90ce377-e246-438c-b973-fe6a0fe13f83",
            },
        )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_turn_never_treats_missing_reply_as_success() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_app(
                httpx.MockTransport(
                    lambda _request: httpx.Response(200, json={"completed": True, "runId": "run-1"})
                )
            )
        ),
        base_url="http://bff.test",
    ) as client:
        response = await client.post(
            "/api/supply-chain/v2/openclaw/turn",
            headers={"Authorization": f"Bearer {_token('tenant-a')}"},
            json={
                "prompt": "什么是供应链管理",
                "client_request_id": "a90ce377-e246-438c-b973-fe6a0fe13f83",
            },
        )
    assert response.status_code == 502
    assert response.json()["error_code"] == "OPENCLAW_REPLY_INVALID"
