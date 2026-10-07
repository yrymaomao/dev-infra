"""Reuse ERP login and existing user Key APIs; no new Java query credential."""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from redis.exceptions import RedisError

from ebiz_deployment.erp_session import (
    ErpSessionPrincipalResolver,
    SessionInvalid,
    SessionUnavailable,
)
from ebiz_deployment.user_mcp_credentials import McpUserKeyResolver, UserMcpRefCodec

SECRET = "test-only-erp-session-secret-at-least-32-bytes"


class Store:
    def __init__(self, uid="alice", cid="222260", *, expiry=300, machine=False):
        self.token = jwt.encode(
            {"KEY": {"USER": uid, "MCP": "0"}, "exp": time.time() + expiry},
            SECRET,
            algorithm="HS256",
        )
        self.values = {
            f"_Cache_Login_User_Data_{uid}": json.dumps(
                {"id": uid, "company": cid, "token": self.token, "isMcp": machine}
            )
        }
        self.error = None

    async def get(self, name):
        if self.error:
            raise self.error
        return self.values.get(name)


class Discovery:
    async def resolve(self, service, group):
        assert (service, group) == ("McpServer", "DEFAULT_GROUP")
        return "http://127.0.0.1:9249"


def session(store):
    return ErpSessionPrincipalResolver(store, jwt_secret=SECRET)


def envelope(result):
    return {"success": True, "code": 200, "result": result}


def row(key_id=8, *, family="ERP", enabled=True):
    return {"keyId": key_id, "serviceType": family, "enabled": enabled}


def detail(key_id=8, **changes):
    return {"keyId": key_id, "serviceType": "ERP", "enabled": 1, "key": "erp-user-key", **changes}


@pytest.mark.asyncio
async def test_current_token_is_verified_and_bound_to_both_identities():
    store = Store()
    client = session(store)
    assert await client.current_token("alice", expected_tenant_id="222260") == store.token
    for uid, cid in (("bob", "222260"), ("alice", "other")):
        with pytest.raises(SessionInvalid):
            await client.current_token(uid, expected_tenant_id=cid)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["expired", "missing", "machine", "wrong-signature", "wrong-uid"])
async def test_current_token_rejects_invalid_login_state(case):
    store = Store(expiry=-1 if case == "expired" else 300, machine=case == "machine")
    if case == "missing":
        store.values.clear()
    elif case in {"wrong-signature", "wrong-uid"}:
        stored = json.loads(store.values["_Cache_Login_User_Data_alice"])
        stored["token"] = jwt.encode(
            {"KEY": {"USER": "bob" if case == "wrong-uid" else "alice"}, "exp": time.time() + 300},
            "wrong-test-key-at-least-32-bytes" if case == "wrong-signature" else SECRET,
            algorithm="HS256",
        )
        store.values["_Cache_Login_User_Data_alice"] = json.dumps(stored)
    with pytest.raises(SessionInvalid):
        await session(store).current_token("alice", expected_tenant_id="222260")


@pytest.mark.asyncio
async def test_current_token_redis_error_does_not_leak_credentials():
    store = Store()
    store.error = RedisError("redis://private:secret@example")
    with pytest.raises(SessionUnavailable) as error:
        await session(store).current_token("alice", expected_tenant_id="222260")
    assert "secret" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", ["alice", "bob"])
async def test_existing_user_list_then_detail_preserves_user_login_and_key(uid):
    store = Store(uid=uid)
    seen = []

    def respond(request):
        seen.append(request.url.path)
        assert request.headers["_token_"] == store.token
        assert request.headers["timezone"] == "0"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert "uid" not in body and "cid" not in body
        if request.url.path == "/erp/mcp-key/list":
            assert body == {"enabled": True, "pageIndex": 1, "pageSize": 100}
            result = {"records": [row(9, family="AGGR"), row()], "total": 2}
        else:
            assert request.url.path == "/erp/mcp-key/detail"
            assert body == {"keyId": 8}
            result = detail(key=f"erp-{uid}-key")
        return httpx.Response(200, json=envelope(result))

    codec = UserMcpRefCodec(b"test-only-signing-secret-at-least-32-bytes")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = McpUserKeyResolver(
            discovery=Discovery(),
            client=client,
            codec=codec,
            service_type="ERP",
            login_session=session(store),
        )
        credential = await resolver.resolve(
            codec.issue("222260", uid),
            provider_id="mcp.streamable_http",
            expected_tenant_id="222260",
        )
    assert credential.secret_value == f"erp-{uid}-key"
    assert credential.tenant_id == "222260"
    assert seen == ["/erp/mcp-key/list", "/erp/mcp-key/detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        detail(key=""),
        detail(keyId=99),
        detail(serviceType="AGGR"),
        detail(enabled=0),
        detail(enabled=True),
        detail(key="secret\r\nheader"),
    ],
)
async def test_invalid_detail_rejects_without_key_or_family_fallback(bad):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        result = {"records": [row()], "total": 1} if len(paths) == 1 else bad
        return httpx.Response(200, json=envelope(result))

    codec = UserMcpRefCodec(b"test-only-signing-secret-at-least-32-bytes")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = McpUserKeyResolver(
            discovery=Discovery(),
            client=client,
            codec=codec,
            service_type="ERP",
            login_session=session(Store()),
        )
        with pytest.raises(ValueError, match="MCP key unavailable"):
            await resolver.resolve(
                codec.issue("222260", "alice"),
                provider_id="mcp.streamable_http",
                expected_tenant_id="222260",
            )
    assert paths == ["/erp/mcp-key/list", "/erp/mcp-key/detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"records": [], "total": 0},
        {"records": [row(family="AGGR")], "total": 1},
        {"records": [row(enabled=False)], "total": 1},
        {"records": [row(key_id=True)], "total": 1},
        {"records": [], "total": 10},
    ],
)
async def test_missing_or_invalid_user_key_never_calls_detail(bad):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=envelope(bad))

    codec = UserMcpRefCodec(b"test-only-signing-secret-at-least-32-bytes")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = McpUserKeyResolver(
            discovery=Discovery(),
            client=client,
            codec=codec,
            service_type="ERP",
            login_session=session(Store()),
        )
        with pytest.raises(ValueError):
            await resolver.resolve(
                codec.issue("222260", "alice"),
                provider_id="mcp.streamable_http",
                expected_tenant_id="222260",
            )
    assert paths == ["/erp/mcp-key/list"]


@pytest.mark.asyncio
async def test_missing_login_fails_before_network():
    def unexpected(request):
        pytest.fail("invalid login reached Key API")

    codec = UserMcpRefCodec(b"test-only-signing-secret-at-least-32-bytes")
    store = Store()
    store.values.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        resolver = McpUserKeyResolver(
            discovery=Discovery(),
            client=client,
            codec=codec,
            service_type="ERP",
            login_session=session(store),
        )
        with pytest.raises(ValueError):
            await resolver.resolve(
                codec.issue("222260", "alice"),
                provider_id="mcp.streamable_http",
                expected_tenant_id="222260",
            )


@pytest.mark.asyncio
async def test_paginated_erp_selection_uses_existing_default_order_and_same_login():
    pages = []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/list"):
            pages.append(body["pageIndex"])
            records = (
                [row(index, family="AGGR") for index in range(1, 101)]
                if body["pageIndex"] == 1
                else [row(101)]
            )
            return httpx.Response(200, json=envelope({"records": records, "total": 101}))
        assert body == {"keyId": 101}
        return httpx.Response(200, json=envelope(detail(101)))

    codec = UserMcpRefCodec(b"test-only-signing-secret-at-least-32-bytes")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = McpUserKeyResolver(
            discovery=Discovery(),
            client=client,
            codec=codec,
            service_type="ERP",
            login_session=session(Store()),
        )
        assert (
            await resolver.resolve(
                codec.issue("222260", "alice"),
                provider_id="mcp.streamable_http",
                expected_tenant_id="222260",
            )
        ).secret_value == "erp-user-key"
    assert pages == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["repeat", "limit", "failure", "timeout"])
async def test_key_lookup_has_bounded_scan_and_no_retry_or_secret_error(mode):
    calls = []

    def respond(request):
        page = json.loads(request.content)["pageIndex"]
        calls.append(page)
        if mode == "timeout":
            raise httpx.ReadTimeout("private token and Key", request=request)
        if mode == "failure":
            return httpx.Response(200, json={"success": False, "code": 500, "result": None})
        offset = 0 if mode == "repeat" else (page - 1) * 100
        records = [row(index + offset, family="AGGR") for index in range(1, 101)]
        return httpx.Response(200, json=envelope({"records": records, "total": 1100}))

    codec = UserMcpRefCodec(b"test-only-signing-secret-at-least-32-bytes")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = McpUserKeyResolver(
            discovery=Discovery(),
            client=client,
            codec=codec,
            service_type="ERP",
            login_session=session(Store()),
        )
        with pytest.raises(ValueError, match="MCP key unavailable") as error:
            await resolver.resolve(
                codec.issue("222260", "alice"),
                provider_id="mcp.streamable_http",
                expected_tenant_id="222260",
            )
        assert error.value.__cause__ is None
        assert "private" not in str(error.value)
    assert len(calls) == {"repeat": 2, "limit": 10, "failure": 1, "timeout": 1}[mode]
