"""ERP identity must come from a current, tenant-bound human login state."""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from redis.exceptions import RedisError


def test_session_settings_require_two_secret_files(monkeypatch, tmp_path):
    module = importlib.import_module("ebiz_deployment.erp_session")
    monkeypatch.delenv("BFF_ERP_SESSION_REDIS_URL_FILE", raising=False)
    with pytest.raises(ValueError, match="BFF_ERP_SESSION_REDIS_URL_FILE"):
        module.ErpSessionSettings.from_environment()
    redis_file = tmp_path / "redis-url"
    secret_file = tmp_path / "jwt-key"
    redis_file.write_text("redis://127.0.0.1:6379/2", encoding="utf-8")
    secret_file.write_text("synthetic-erp-secret", encoding="utf-8")
    monkeypatch.setenv("BFF_ERP_SESSION_REDIS_URL_FILE", str(redis_file))
    monkeypatch.setenv("BFF_ERP_SESSION_JWT_SECRET_FILE", str(secret_file))
    settings = module.ErpSessionSettings.from_environment()
    assert settings.timeout_seconds == 2
    assert "synthetic-erp-secret" not in repr(settings)
    assert "redis://" not in repr(settings)
    monkeypatch.setenv("BFF_ERP_SESSION_TIMEOUT_SECONDS", "nan")
    with pytest.raises(ValueError, match="ERP session"):
        module.ErpSessionSettings.from_environment()


@pytest.mark.asyncio
async def test_session_pool_closes_when_application_assembly_fails():
    module = importlib.import_module("ebiz_deployment.erp_session")
    store = AsyncMock()
    settings = module.ErpSessionSettings("redis://127.0.0.1:6379/2", SECRET)
    with patch.object(module.Redis, "from_url", return_value=store) as factory:
        with pytest.raises(RuntimeError, match="assembly failed"):
            async with module.open_erp_session(settings) as client:
                assert isinstance(client, module.ErpSessionPrincipalResolver)
                raise RuntimeError("assembly failed")
    assert factory.call_count == 1
    store.aclose.assert_awaited_once()


SECRET = "test-only-erp-jwt-key-with-at-least-32-bytes"


def token(*, uid: str = "alice", machine: str = "0", expiry: int = 300) -> str:
    return jwt.encode(
        {"KEY": {"USER": uid, "MCP": machine}, "exp": int(time.time()) + expiry},
        SECRET,
        algorithm="HS256",
    )


class LoginStore:
    def __init__(self, values: dict[str, str | bytes | None]) -> None:
        self.values = values
        self.reads: list[str] = []
        self.error: Exception | None = None

    async def get(self, key: str) -> str | bytes | None:
        self.reads.append(key)
        if self.error:
            raise self.error
        return self.values.get(key)


def resolver(values: dict[str, str | bytes | None], *, timeout: float = 2):
    module = importlib.import_module("ebiz_deployment.erp_session")
    store = LoginStore(values)
    return module.ErpSessionPrincipalResolver(
        store, jwt_secret=SECRET, timeout_seconds=timeout
    ), store


def state(current: str, **overrides: object) -> str:
    return json.dumps(
        {
            "id": "alice",
            "company": "222260",
            "token": current,
            "isMcp": False,
            "role": ["CRM_AGENT", "USER", "bad role", 9],
            **overrides,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["plain", "double", "bytes"])
async def test_current_human_login_returns_only_trusted_identity(encoding: str) -> None:
    presented = token()
    raw: str | bytes = state(presented)
    if encoding == "double":
        raw = json.dumps(raw)
    elif encoding == "bytes":
        raw = raw.encode()
    client, store = resolver({"_Cache_Login_User_Data_alice": raw})
    principal = await client.resolve(presented)
    assert principal.tenant_id == "222260"
    assert principal.actor_id == "alice"
    assert principal.roles == ("CRM_AGENT", "USER")
    assert store.reads == ["_Cache_Login_User_Data_alice"]
    assert presented not in repr(principal)


@pytest.mark.asyncio
async def test_only_the_still_cached_previous_token_is_accepted() -> None:
    old = token(expiry=100)
    current = token(expiry=200)
    client, store = resolver(
        {"_Cache_Login_User_Data_alice": state(current), "_Cache_Old_Token_alice": old}
    )
    assert (await client.resolve(old)).actor_id == "alice"
    assert store.reads == ["_Cache_Login_User_Data_alice", "_Cache_Old_Token_alice"]
    del store.values["_Cache_Old_Token_alice"]
    with pytest.raises(
        importlib.import_module("ebiz_deployment.erp_session").SessionInvalid
    ) as error:
        await client.resolve(old)
    assert error.value.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_token", ["expired", "wrong-signature", "malformed", "machine", "no-exp"]
)
async def test_invalid_jwt_never_reads_the_login_store(bad_token: str) -> None:
    presented = token(expiry=-1) if bad_token == "expired" else token()
    if bad_token == "wrong-signature":
        presented = jwt.encode(
            {"KEY": {"USER": "alice"}, "exp": time.time() + 300},
            "wrong-test-key-with-at-least-32-bytes",
            algorithm="HS256",
        )
    elif bad_token == "malformed":
        presented = "not-a-jwt"
    elif bad_token == "machine":
        presented = token(machine="1")
    elif bad_token == "no-exp":
        presented = jwt.encode({"KEY": {"USER": "alice"}}, SECRET, algorithm="HS256")
    client, store = resolver({})
    with pytest.raises(
        importlib.import_module("ebiz_deployment.erp_session").SessionInvalid
    ) as error:
        await client.resolve(presented)
    assert error.value.status == (403 if bad_token == "machine" else 401)
    assert store.reads == []
    assert presented not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", 401),
        ("malformed", 401),
        ("wrong-user", 401),
        ("no-tenant", 403),
        ("machine", 403),
        ("stale", 401),
    ],
)
async def test_invalid_cached_identity_is_rejected(case: str, status: int) -> None:
    presented = token()
    values: dict[str, str | bytes | None] = {"_Cache_Login_User_Data_alice": state(presented)}
    if case == "missing":
        values.clear()
    elif case == "malformed":
        values["_Cache_Login_User_Data_alice"] = "broken-json"
    elif case == "wrong-user":
        values["_Cache_Login_User_Data_alice"] = state(presented, id="bob")
    elif case == "no-tenant":
        values["_Cache_Login_User_Data_alice"] = state(presented, company="")
    elif case == "machine":
        values["_Cache_Login_User_Data_alice"] = state(presented, isMcp=True)
    elif case == "stale":
        values["_Cache_Login_User_Data_alice"] = state("another-token")
    client, _ = resolver(values)
    with pytest.raises(
        importlib.import_module("ebiz_deployment.erp_session").SessionInvalid
    ) as error:
        await client.resolve(presented)
    assert error.value.status == status


@pytest.mark.asyncio
async def test_redis_error_is_unavailable_not_invalid_and_does_not_leak_details() -> None:
    client, store = resolver({})
    store.error = RedisError("redis://user:secret@example:6379")
    with pytest.raises(
        importlib.import_module("ebiz_deployment.erp_session").SessionUnavailable
    ) as error:
        await client.resolve(token())
    assert "secret" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_entire_login_lookup_has_a_bounded_timeout() -> None:
    module = importlib.import_module("ebiz_deployment.erp_session")

    class SlowStore:
        async def get(self, key: str) -> None:
            await asyncio.sleep(1)

    client = module.ErpSessionPrincipalResolver(
        SlowStore(), jwt_secret=SECRET, timeout_seconds=0.01
    )
    with pytest.raises(module.SessionUnavailable):
        await client.resolve(token())
