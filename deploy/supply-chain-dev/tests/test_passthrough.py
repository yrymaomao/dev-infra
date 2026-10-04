"""Deployment boundary tests; no dev connection or real credentials."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

MODULE = Path(__file__).resolve().parents[1] / "passthrough.py"
spec = importlib.util.spec_from_file_location("sc_dev_passthrough", MODULE)
assert spec and spec.loader
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


def discovery(host="192.168.10.18", port=9249, **fields):
    return {
        "hosts": [
            {"ip": host, "port": port, "healthy": True, "enabled": True, "weight": 1, **fields}
        ]
    }


@pytest.mark.parametrize(
    "body",
    [discovery(), discovery(healthy=False), discovery(port=9252), discovery(host="evil.test")],
)
def test_discovery_rewrites_only_approved_instance(body):
    result = proxy.rewrite_discovery(body)
    if (
        body["hosts"][0]["healthy"]
        and body["hosts"][0]["port"] == 9249
        and body["hosts"][0]["ip"] == "192.168.10.18"
    ):
        assert result["hosts"][0]["ip"] == "127.0.0.1"
        assert result["hosts"][0]["port"] == 18249
        assert body["hosts"][0]["ip"] == "192.168.10.18"
    else:
        assert result["hosts"] == []


@pytest.mark.asyncio
async def test_mcp_forwards_key_session_and_bytes_without_global_fallback():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            200,
            content=b'data: {"ok":true}\n\n',
            headers={"content-type": "text/event-stream", "mcp-session-id": "session-1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        app = proxy.create_app("mcp", outbound)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as browser:
            for key in ("user-a", "user-b"):
                response = await browser.post(
                    "/mcp",
                    content=b'{"jsonrpc":"2.0"}',
                    headers={"X-Mcp-Key": key, "Mcp-Session-Id": "session-1"},
                )
                assert response.content == b'data: {"ok":true}\n\n'
                assert response.headers["mcp-session-id"] == "session-1"
            assert [r.headers["x-mcp-key"] for r in seen] == ["user-a", "user-b"]
            assert seen[0].content == b'{"jsonrpc":"2.0"}'
            assert str(seen[0].url) == "http://192.168.10.18:9249/mcp"
            assert (await browser.get("/mcp")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/nacos/v1/cs/configs"),
        ("POST", "/v1/resolve"),
        ("GET", "/admin"),
        ("POST", "/mcp/other"),
    ],
)
async def test_other_paths_do_not_reach_upstream(method, path):
    def upstream(request):
        pytest.fail("unauthorized upstream request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.create_app("mcp", outbound)),
            base_url="http://local",
        ) as browser:
            assert (await browser.request(method, path)).status_code == 404


@pytest.mark.asyncio
async def test_nacos_rejects_other_service_and_preserves_query():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json=discovery())

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.create_app("nacos", outbound)),
            base_url="http://local",
        ) as browser:
            path = "/nacos/v1/ns/instance/list"
            assert (await browser.get(path, params={"serviceName": "Other"})).status_code == 403
            response = await browser.get(
                path, params={"serviceName": "DEFAULT_GROUP@@McpServer", "healthyOnly": "true"}
            )
            assert response.json()["hosts"][0]["port"] == 18249
            assert len(seen) == 1
            assert seen[0].url.params["serviceName"] == "DEFAULT_GROUP@@McpServer"


@pytest.mark.asyncio
async def test_key_query_is_transparent_and_redirect_is_not_followed():
    def upstream(request):
        assert json.loads(request.content) == {"cid": "tenant", "uid": "user"}
        return httpx.Response(307, headers={"location": "http://evil.test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.create_app("mcp", outbound)),
            base_url="http://local",
        ) as browser:
            response = await browser.post(
                "/erp/feign/mcp/key/query/aggr-key", json={"cid": "tenant", "uid": "user"}
            )
            assert response.status_code == 502


@pytest.mark.asyncio
async def test_non_loopback_caller_is_rejected():
    def upstream(request):
        pytest.fail("untrusted caller reached upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        transport = httpx.ASGITransport(
            app=proxy.create_app("mcp", outbound), client=("10.1.1.1", 1)
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as browser:
            assert (await browser.post("/mcp", headers={"X-Mcp-Key": "sample"})).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_session_resume_and_close(method):
    def upstream(request):
        assert request.method == method
        assert request.headers["mcp-session-id"] == "session-1"
        assert request.headers["last-event-id"] == "cursor-2"
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.create_app("mcp", outbound)),
            base_url="http://local",
        ) as browser:
            response = await browser.request(
                method,
                "/mcp",
                headers={
                    "X-Mcp-Key": "user-a",
                    "Mcp-Session-Id": "session-1",
                    "Last-Event-Id": "cursor-2",
                },
            )
            assert response.status_code == 200


@pytest.mark.asyncio
async def test_nacos_login_passthrough_does_not_store_credentials():
    payload = b"username=fixture&password=fixture-only"

    def upstream(request):
        assert request.content == payload
        return httpx.Response(200, json={"accessToken": "fixture-only"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        app = proxy.create_app("nacos", outbound)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as browser:
            assert (await browser.post(proxy.LOGIN_PATH, content=payload)).status_code == 200
            assert set(app.state._state) == {"client"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/m%63p", "/%2fmcp", "/mcp%5cother"])
async def test_encoded_path_is_not_forwarded(path):
    def upstream(request):
        pytest.fail("encoded path reached upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.create_app("mcp", outbound)),
            base_url="http://local",
        ) as browser:
            assert (await browser.post(path, headers={"X-Mcp-Key": "fixture"})).status_code == 404


@pytest.mark.asyncio
async def test_timeout_is_safe_and_not_retried():
    seen = []

    def upstream(request):
        seen.append(request)
        raise httpx.ReadTimeout("secret fixture must not leak", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.create_app("mcp", outbound)),
            base_url="http://local",
        ) as browser:
            response = await browser.post("/mcp", headers={"X-Mcp-Key": "fixture"})
            assert response.status_code == 502
            assert response.content == b""
            assert len(seen) == 1
