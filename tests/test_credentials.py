from __future__ import annotations

import importlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest


def credentials_module() -> Any:
    return importlib.import_module("ebiz_deployment.credentials")


class BrokerServer:
    def __init__(
        self,
        responses: list[tuple[int, dict[str, object] | str]],
        *,
        delay_seconds: float = 0,
    ) -> None:
        self.responses = responses
        self.delay_seconds = delay_seconds
        self.requests: list[dict[str, object]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                owner.requests.append(
                    {
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                time.sleep(owner.delay_seconds)
                index = min(len(owner.requests) - 1, len(owner.responses) - 1)
                status, response = owner.responses[index]
                encoded = (
                    json.dumps(response).encode("utf-8")
                    if isinstance(response, dict)
                    else response.encode("utf-8")
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1/resolve"

    def __enter__(self) -> BrokerServer:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


def future_expiry(seconds: int = 120) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


async def test_environment_secret_resolver_enforces_symbolic_allowlist_and_redacts() -> None:
    resolver = credentials_module().EnvironmentSecretResolver(
        {"model_key": "MODEL_KEY_ENV"}, {"MODEL_KEY_ENV": "top-secret-model-value"}
    )

    assert await resolver.resolve("model_key") == "top-secret-model-value"
    with pytest.raises(ValueError) as error:
        await resolver.resolve("other_key")

    assert "top-secret-model-value" not in repr(resolver)
    assert "top-secret-model-value" not in str(error.value)


async def test_broker_resolves_per_call_over_real_http_without_cache() -> None:
    responses = [
        (
            200,
            {
                "provider_id": "yeaher.erp",
                "access_token": "token-one",
                "expires_at": future_expiry(),
            },
        ),
        (
            200,
            {
                "provider_id": "yeaher.erp",
                "access_token": "token-two",
                "expires_at": future_expiry(),
            },
        ),
    ]
    with BrokerServer(responses) as server:
        secret_resolver = credentials_module().EnvironmentSecretResolver(
            {"broker_auth": "BROKER_AUTH_ENV"}, {"BROKER_AUTH_ENV": "broker-auth-value"}
        )
        resolver = credentials_module().HttpsCredentialBrokerResolver(
            url=server.url,
            auth_secret_name="broker_auth",
            allowed_provider_ids=("yeaher.erp",),
            timeout_seconds=2,
            secret_resolver=secret_resolver,
        )

        first = await resolver.resolve("opaque:first", provider_id="yeaher.erp")
        second = await resolver.resolve("opaque:first", provider_id="yeaher.erp")

    assert (first, second) == ("token-one", "token-two")
    assert len(server.requests) == 2
    assert server.requests[0]["body"] == {
        "credential_ref": "opaque:first",
        "provider_id": "yeaher.erp",
    }
    assert server.requests[0]["authorization"] == "Bearer broker-auth-value"
    assert "opaque:first" not in repr(resolver)
    assert "token-two" not in repr(resolver)


async def test_bound_broker_rejects_tenant_mismatch_and_returns_registry_binding() -> None:
    response = {
        "provider_id": "mcp.streamable_http",
        "access_token": "mcp-key-value",
        "tenant_id": "tenant-a",
        "tenant_binding_digest": "a" * 64,
        "expires_at": future_expiry(),
    }
    with BrokerServer([(200, response)]) as server:
        secret_resolver = credentials_module().EnvironmentSecretResolver(
            {"broker_auth": "BROKER_AUTH_ENV"}, {"BROKER_AUTH_ENV": "broker-auth-value"}
        )
        resolver = credentials_module().HttpsBoundCredentialBrokerResolver(
            url=server.url,
            auth_secret_name="broker_auth",
            allowed_provider_ids=("mcp.streamable_http",),
            timeout_seconds=2,
            secret_resolver=secret_resolver,
        )

        resolved = await resolver.resolve(
            "opaque:first",
            provider_id="mcp.streamable_http",
            expected_tenant_id="tenant-a",
        )
        with pytest.raises(ValueError, match="credential broker resolution failed"):
            await resolver.resolve(
                "opaque:first",
                provider_id="mcp.streamable_http",
                expected_tenant_id="tenant-b",
            )

    assert resolved.tenant_id == "tenant-a"
    assert resolved.tenant_binding_digest == "a" * 64
    assert resolved.secret_value == "mcp-key-value"
    assert server.requests[0]["body"] == {
        "credential_ref": "opaque:first",
        "provider_id": "mcp.streamable_http",
        "expected_tenant_id": "tenant-a",
    }


async def test_broker_concurrent_calls_each_hit_real_http() -> None:
    responses = [
        (
            200,
            {
                "provider_id": "mcp.streamable_http",
                "access_token": f"token-{index}",
                "expires_at": future_expiry(),
            },
        )
        for index in range(8)
    ]
    with BrokerServer(responses) as server:
        secret_resolver = credentials_module().EnvironmentSecretResolver(
            {"broker_auth": "BROKER_AUTH_ENV"}, {"BROKER_AUTH_ENV": "broker-auth-value"}
        )
        resolver = credentials_module().HttpsCredentialBrokerResolver(
            url=server.url,
            auth_secret_name="broker_auth",
            allowed_provider_ids=("mcp.streamable_http",),
            timeout_seconds=2,
            secret_resolver=secret_resolver,
        )

        import asyncio

        tokens = await asyncio.gather(
            *(
                resolver.resolve(f"opaque:{index}", provider_id="mcp.streamable_http")
                for index in range(8)
            )
        )

    assert len(tokens) == 8
    assert len(server.requests) == 8


async def test_broker_timeout_is_bounded_and_sanitized() -> None:
    response = {
        "provider_id": "yeaher.erp",
        "access_token": "late-token",
        "expires_at": future_expiry(),
    }
    with BrokerServer([(200, response)], delay_seconds=0.3) as server:
        secret_resolver = credentials_module().EnvironmentSecretResolver(
            {"broker_auth": "BROKER_AUTH_ENV"}, {"BROKER_AUTH_ENV": "broker-auth-value"}
        )
        resolver = credentials_module().HttpsCredentialBrokerResolver(
            url=server.url,
            auth_secret_name="broker_auth",
            allowed_provider_ids=("yeaher.erp",),
            timeout_seconds=0.05,
            secret_resolver=secret_resolver,
        )
        started = time.monotonic()

        with pytest.raises(ValueError) as error:
            await resolver.resolve("timeout-sensitive-ref", provider_id="yeaher.erp")

        elapsed = time.monotonic() - started
    assert elapsed < 0.25
    assert "timeout-sensitive-ref" not in str(error.value)
    assert "late-token" not in str(error.value)


@pytest.mark.parametrize(
    "status,response",
    [
        (500, {"error": "upstream included sensitive-ref"}),
        (200, "not-json"),
        (
            200,
            {"provider_id": "other", "access_token": "leaked-token", "expires_at": future_expiry()},
        ),
        (
            200,
            {
                "provider_id": "yeaher.erp",
                "access_token": "expired-token",
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
        ),
    ],
)
async def test_broker_failures_are_sanitized(
    status: int, response: dict[str, object] | str
) -> None:
    with BrokerServer([(status, response)]) as server:
        secret_resolver = credentials_module().EnvironmentSecretResolver(
            {"broker_auth": "BROKER_AUTH_ENV"}, {"BROKER_AUTH_ENV": "broker-auth-value"}
        )
        resolver = credentials_module().HttpsCredentialBrokerResolver(
            url=server.url,
            auth_secret_name="broker_auth",
            allowed_provider_ids=("yeaher.erp",),
            timeout_seconds=2,
            secret_resolver=secret_resolver,
        )

        with pytest.raises(ValueError) as error:
            await resolver.resolve("sensitive-ref", provider_id="yeaher.erp")

    rendered = str(error.value)
    assert "sensitive-ref" not in rendered
    assert "leaked-token" not in rendered
    assert "expired-token" not in rendered
    assert "broker-auth-value" not in rendered
