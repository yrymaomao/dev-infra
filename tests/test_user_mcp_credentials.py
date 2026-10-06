"""User-scoped MCP key resolution never trusts a caller-selected user."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ebiz_deployment.local_dev_services import create_mcp_app, create_provider_app
from ebiz_deployment.service_discovery import NacosServiceResolver
from ebiz_deployment.user_mcp_credentials import McpUserKeyResolver, UserMcpRefCodec


def _codec() -> UserMcpRefCodec:
    return UserMcpRefCodec(b"test-only-signing-key-with-at-least-32-bytes")


def test_user_reference_is_bound_to_tenant_and_detects_tampering() -> None:
    codec = _codec()
    reference = codec.issue("222260", "alice")
    assert len(reference) <= 256
    assert codec.parse(reference, expected_tenant_id="222260") == "alice"
    with pytest.raises(ValueError):
        codec.parse(reference, expected_tenant_id="another-tenant")
    with pytest.raises(ValueError):
        codec.parse(
            reference[:-1] + ("0" if reference[-1] != "0" else "1"),
            expected_tenant_id="222260",
        )
    with pytest.raises(ValueError):
        codec.parse("opaque:global-key", expected_tenant_id="222260")


def test_user_reference_cannot_shift_a_separator_between_tenant_and_user() -> None:
    codec = _codec()
    reference = codec.issue("tenant", "part\x00alice")
    signature = reference.rsplit(".", 1)[1]
    forged = f"mcpuser.v1.{b'alice'.hex()}.{signature}"
    with pytest.raises(ValueError):
        codec.parse(forged, expected_tenant_id="tenant\x00part")


def test_resolver_uses_trusted_identity_and_returns_only_the_mcp_key() -> None:
    calls: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/erp/feign/mcp/key/query/aggr-key"
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"success": True, "code": 200, "result": {"keyId": 7, "mcpKey": "user-key"}},
        )

    codec = _codec()
    resolver = McpUserKeyResolver(
        discovery=_discovery(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        codec=codec,
    )
    resolved = asyncio.run(
        resolver.resolve(
            codec.issue("222260", "alice"),
            provider_id="mcp.streamable_http",
            expected_tenant_id="222260",
        )
    )
    assert calls == [{"cid": "222260", "uid": "alice"}]
    assert resolved.secret_value == "user-key"
    assert resolved.tenant_id == "222260"
    assert len(resolved.tenant_binding_digest) == 64


@pytest.mark.parametrize(
    "response",
    [
        {"success": True, "code": 200, "result": None},
        {"success": False, "code": 500, "result": {"mcpKey": "must-not-use"}},
        {"success": True, "code": 500, "result": {"mcpKey": "must-not-use"}},
        {"success": True, "code": 200, "result": {"keyId": 1}},
        {"success": True, "code": 200, "result": {"mcpKey": " "}},
    ],
)
def test_resolver_fails_closed_on_missing_or_failed_key(response: dict[str, object]) -> None:
    codec = _codec()
    resolver = McpUserKeyResolver(
        discovery=_discovery(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
        ),
        codec=codec,
    )
    with pytest.raises(ValueError, match="MCP key unavailable"):
        asyncio.run(
            resolver.resolve(
                codec.issue("222260", "alice"),
                provider_id="mcp.streamable_http",
                expected_tenant_id="222260",
            )
        )


def test_resolver_rejects_other_provider_before_discovery() -> None:
    codec = _codec()
    resolver = McpUserKeyResolver(discovery=_discovery(), client=httpx.AsyncClient(), codec=codec)
    with pytest.raises(ValueError):
        asyncio.run(
            resolver.resolve(
                codec.issue("222260", "alice"),
                provider_id="yeaher.erp",
                expected_tenant_id="222260",
            )
        )


def test_erp_resolver_uses_authenticated_erp_query_without_aggr_fallback() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer " + "t" * 40
        assert json.loads(request.content) == {"cid": "222260", "uid": "alice"}
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 200,
                "result": {
                    "keyId": 8,
                    "cid": "222260",
                    "uid": "alice",
                    "mcpKey": "erp-user-key",
                },
            },
        )

    codec = _codec()
    resolver = McpUserKeyResolver(
        discovery=_discovery(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        codec=codec,
        service_type="ERP",
        query_token="t" * 40,
    )
    resolved = asyncio.run(
        resolver.resolve(
            codec.issue("222260", "alice"),
            provider_id="mcp.streamable_http",
            expected_tenant_id="222260",
        )
    )
    assert calls == ["/erp/feign/mcp/key/query/erp-key"]
    assert resolved.secret_value == "erp-user-key"


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"cid": "other", "uid": "alice", "mcpKey": "secret"},
        {"cid": "222260", "uid": "bob", "mcpKey": "secret"},
        {"mcpKey": "secret"},
    ],
)
def test_erp_resolver_rejects_missing_key_or_mismatched_identity_without_retry(result) -> None:
    paths: list[str] = []

    def respond(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"success": True, "code": 200, "result": result})

    codec = _codec()
    resolver = McpUserKeyResolver(
        discovery=_discovery(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        codec=codec,
        service_type="ERP",
        query_token="t" * 40,
    )
    with pytest.raises(ValueError, match="MCP key unavailable"):
        asyncio.run(
            resolver.resolve(
                codec.issue("222260", "alice"),
                provider_id="mcp.streamable_http",
                expected_tenant_id="222260",
            )
        )
    assert paths == ["/erp/feign/mcp/key/query/erp-key"]


def test_erp_query_requires_internal_authentication_configuration() -> None:
    with pytest.raises(ValueError):
        McpUserKeyResolver(
            discovery=_discovery(), client=httpx.AsyncClient(), codec=_codec(), service_type="ERP"
        )


def test_query_configuration_requires_erp_token_and_rejects_unknown_service() -> None:
    from ebiz_deployment.config import UserMcpKeyQueryConfig

    values = dict(
        signing_secret_name="signer",
        timeout_seconds=5,
        server_addresses="127.0.0.1:8848",
        allowed_hosts=("127.0.0.1",),
    )
    with pytest.raises(ValueError):
        UserMcpKeyQueryConfig(**values, service_type="ERP")
    with pytest.raises(ValueError):
        UserMcpKeyQueryConfig(**values, service_type="ANY")
    config = UserMcpKeyQueryConfig(**values, service_type="ERP", token_secret_name="query_token")
    assert config.secret_names == ("signer", "query_token")


def test_local_user_key_query_grants_mcp_tools_list_without_a_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ebiz_deployment.local_dev_services import LOCAL_TENANT_ID, LOCAL_USER_REF_SIGNING_KEY

    monkeypatch.setenv("SUPPLY_CHAIN_SNAPSHOT_TIME", "2026-08-30T00:00:00Z")
    codec = UserMcpRefCodec(LOCAL_USER_REF_SIGNING_KEY.encode())
    resolver = McpUserKeyResolver(
        discovery=_discovery(port=18082),
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_provider_app())),
        codec=codec,
    )
    resolved = asyncio.run(
        resolver.resolve(
            codec.issue(LOCAL_TENANT_ID, "local-dev-user"),
            provider_id="mcp.streamable_http",
            expected_tenant_id=LOCAL_TENANT_ID,
        )
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "credential-chain-test", "version": "1"},
        },
    }
    with TestClient(create_mcp_app(), base_url="http://127.0.0.1:18081") as client:
        headers = {
            "X-Mcp-Key": resolved.secret_value,
            "Accept": "application/json, text/event-stream",
        }
        initialized = client.post(
            "/mcp",
            json=request,
            headers=headers,
        )
        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
    assert initialized.status_code == listed.status_code == 200
    assert "query_inventory_summary_v2" in listed.text


class LocalNaming:
    def __init__(self, port):
        self.port = port

    def list_naming_instance(self, *args, **kwargs):
        return {
            "hosts": [
                {
                    "ip": "127.0.0.1",
                    "port": self.port,
                    "healthy": True,
                    "enabled": True,
                    "weight": 1,
                    "metadata": {"protocol": "http"},
                }
            ]
        }


def _discovery(port=9999):
    return NacosServiceResolver(LocalNaming(port), allowed_hosts=("127.0.0.1",))
