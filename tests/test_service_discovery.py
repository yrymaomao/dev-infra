"""Nacos discovery filters, bounded caching and secure egress."""

import asyncio
import importlib

import pytest


class Naming:
    def __init__(self, hosts):
        self.hosts = hosts
        self.calls = []

    def list_naming_instance(self, service_name, **kwargs):
        self.calls.append((service_name, kwargs))
        return {"hosts": self.hosts}


def host(**changes):
    return {
        "ip": "mcp.example.test",
        "port": 443,
        "healthy": True,
        "enabled": True,
        "weight": 1,
        "metadata": {"protocol": "https"},
        **changes,
    }


def resolver(client, clock=lambda: 0.0):
    module = importlib.import_module("ebiz_deployment.service_discovery")
    return module.NacosServiceResolver(client, allowed_hosts=("mcp.example.test",), clock=clock)


@pytest.mark.asyncio
async def test_filters_instances_and_uses_five_second_cache():
    naming = Naming([host(healthy=False), host(enabled=False), host(weight=0), host()])
    now = [0.0]
    service = resolver(naming, lambda: now[0])
    assert await service.resolve("McpServer", "DEFAULT_GROUP") == "https://mcp.example.test:443"
    assert await service.resolve("McpServer", "DEFAULT_GROUP") == "https://mcp.example.test:443"
    assert len(naming.calls) == 1
    now[0] = 5.0
    naming.hosts = []
    with pytest.raises(ValueError, match="service unavailable"):
        await service.resolve("McpServer", "DEFAULT_GROUP")
    assert len(naming.calls) == 2
    assert naming.calls[0][1] == {"group_name": "DEFAULT_GROUP", "healthy_only": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hosts",
    [
        [],
        [host(weight=-1)],
        [host(weight="bad")],
        [host(ip="unapproved.test")],
        [host(metadata={"protocol": "http"})],
        [host(port=0)],
        [host(ip="user:secret@mcp.example.test")],
    ],
)
async def test_invalid_or_unsafe_instances_fail_closed(hosts):
    with pytest.raises(ValueError, match="service unavailable"):
        await resolver(Naming(hosts)).resolve("McpServer", "DEFAULT_GROUP")


@pytest.mark.asyncio
async def test_parallel_requests_share_refresh_and_do_not_block_event_loop():
    import time

    class Slow(Naming):
        def list_naming_instance(self, service_name, **kwargs):
            time.sleep(0.02)
            return super().list_naming_instance(service_name, **kwargs)

    naming = Slow([host()])
    service = resolver(naming)
    await asyncio.gather(*(service.resolve("McpServer", "DEFAULT_GROUP") for _ in range(8)))
    assert len(naming.calls) == 1


@pytest.mark.asyncio
async def test_failed_group_refresh_invalidates_previous_group_cache():
    naming = Naming([host()])
    service = resolver(naming)
    await service.resolve("McpServer", "DEFAULT_GROUP")
    naming.hosts = []
    with pytest.raises(ValueError, match="service unavailable"):
        await service.resolve("McpServer", "OTHER_GROUP")
    with pytest.raises(ValueError, match="service unavailable"):
        await service.resolve("McpServer", "DEFAULT_GROUP")
    assert len(naming.calls) == 3


@pytest.mark.asyncio
async def test_discovery_timeout_and_exception_hide_upstream_details():
    class Broken:
        def list_naming_instance(self, *args, **kwargs):
            raise RuntimeError("secret upstream response")

    with pytest.raises(ValueError, match="^MCP service unavailable$") as error:
        await resolver(Broken()).resolve("McpServer", "DEFAULT_GROUP")
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_discovery_times_out_without_using_stale_instances():
    import time

    class Slow(Naming):
        def list_naming_instance(self, *args, **kwargs):
            time.sleep(0.05)
            return super().list_naming_instance(*args, **kwargs)

    module = importlib.import_module("ebiz_deployment.service_discovery")
    service = module.NacosServiceResolver(
        Slow([host()]), allowed_hosts=("mcp.example.test",), timeout_seconds=0.005
    )
    with pytest.raises(ValueError, match="service unavailable"):
        await service.resolve("McpServer", "DEFAULT_GROUP")


@pytest.mark.asyncio
async def test_key_lookup_discovers_endpoint_and_reuses_pool_without_caching_key():
    import httpx

    from ebiz_deployment.user_mcp_credentials import McpUserKeyResolver, UserMcpRefCodec

    codec = UserMcpRefCodec(b"s" * 32)
    naming = Naming([host()])
    seen = []

    def respond(request):
        import json

        identity = json.loads(request.content)
        seen.append(identity)
        return httpx.Response(
            200, json={"success": True, "code": 200, "result": {"mcpKey": identity["uid"] + "-key"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        lookup = McpUserKeyResolver(discovery=resolver(naming), client=http, codec=codec)
        for uid in ("alice", "bob", "alice"):
            result = await lookup.resolve(
                codec.issue("222260", uid),
                provider_id="mcp.streamable_http",
                expected_tenant_id="222260",
            )
            assert result.secret_value == uid + "-key"
        assert not http.is_closed
    assert [call["uid"] for call in seen] == ["alice", "bob", "alice"]


def test_real_sdk_configuration_uses_supported_options_without_logging_secrets(tmp_path):
    module = importlib.import_module("ebiz_deployment.service_discovery")
    import logging

    client = module.create_nacos_client(
        "127.0.0.1:8848",
        namespace="fixture",
        username="synthetic-user",
        password="synthetic-password",
        timeout_seconds=2,
    )
    assert client.default_timeout == 2
    assert client.username == "synthetic-user"
    assert client.namespace == "fixture"
    assert logging.getLogger("nacos.client").disabled


@pytest.mark.asyncio
@pytest.mark.parametrize("erp", [False, True])
async def test_query_pool_closes_on_start_failure_and_normal_shutdown(erp):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from ebiz_deployment.composition import UserKeyCompositionRoot
    from ebiz_deployment.erp_session import ErpSessionPrincipalResolver, ErpSessionSettings

    store = AsyncMock()
    redis = AsyncMock()
    settings = ErpSessionSettings("redis://127.0.0.1:6379/2", "test-secret") if erp else None
    base = SimpleNamespace(snapshot=object(), aclose=AsyncMock())
    factory = SimpleNamespace(start=AsyncMock(return_value=base))

    def build(client, login):
        assert client is store
        assert isinstance(login, ErpSessionPrincipalResolver) if erp else login is None
        return factory

    with (
        patch("ebiz_deployment.composition.httpx.AsyncClient", return_value=store),
        patch("ebiz_deployment.erp_session.Redis.from_url", return_value=redis),
    ):
        root = UserKeyCompositionRoot(build, settings)
        composition = await root.start()
        assert composition.snapshot is base.snapshot
        await composition.aclose()
        await composition.aclose()
        store.aclose.assert_awaited_once()
        base.aclose.assert_awaited_once()
        if erp:
            redis.aclose.assert_awaited_once()
    store = AsyncMock()
    redis = AsyncMock()
    factory.start = AsyncMock(side_effect=RuntimeError("start failure"))
    with (
        patch("ebiz_deployment.composition.httpx.AsyncClient", return_value=store),
        patch("ebiz_deployment.erp_session.Redis.from_url", return_value=redis),
    ):
        with pytest.raises(RuntimeError, match="start failure"):
            await UserKeyCompositionRoot(build, settings).start()
        store.aclose.assert_awaited_once()
        if erp:
            redis.aclose.assert_awaited_once()
