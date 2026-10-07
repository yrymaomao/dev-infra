"""Shared, read-only ERP human-session resolution for Deployment ingress."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
import warnings
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import jwt
from jwt.warnings import InsecureKeyLengthWarning
from redis.asyncio import Redis
from redis.exceptions import RedisError

_ROLE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_MAX_TOKEN = 4096
_MAX_DOCUMENT = 65536


def _secret_file(name: str, environ: Mapping[str, str]) -> str:
    filename = environ.get(name, "").strip()
    if not filename:
        raise ValueError(f"{name} is required")
    try:
        with Path(filename).open("rb") as source:
            raw = source.read(8193)
        value = raw.decode("utf-8").strip()
    except (OSError, UnicodeError):
        raise ValueError(f"{name} could not be loaded") from None
    if not value or len(raw) > 8192:
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ErpSessionSettings:
    redis_url: str = field(repr=False)
    jwt_secret: str = field(repr=False)
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.redis_url)
        if (
            parsed.scheme not in {"redis", "rediss"}
            or not parsed.hostname
            or parsed.fragment
            or not self.jwt_secret
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 30
        ):
            raise ValueError("ERP session settings are invalid")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ErpSessionSettings:
        values = os.environ if environ is None else environ
        redis_url = _secret_file("BFF_ERP_SESSION_REDIS_URL_FILE", values)
        secret = _secret_file("BFF_ERP_SESSION_JWT_SECRET_FILE", values)
        try:
            timeout = float(values.get("BFF_ERP_SESSION_TIMEOUT_SECONDS", "2"))
        except ValueError:
            raise ValueError("ERP session timeout is invalid") from None
        return cls(redis_url, secret, timeout)


@asynccontextmanager
async def open_erp_session(
    settings: ErpSessionSettings | None,
) -> AsyncIterator[ErpSessionPrincipalResolver | None]:
    """Own a single login-store pool, including failed application assembly."""
    if settings is None:
        yield None
        return
    store = Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_timeout=settings.timeout_seconds,
        socket_connect_timeout=settings.timeout_seconds,
    )
    try:
        yield ErpSessionPrincipalResolver(
            store, jwt_secret=settings.jwt_secret, timeout_seconds=settings.timeout_seconds
        )
    finally:
        await store.aclose()


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    tenant_id: str
    actor_id: str
    roles: tuple[str, ...]


class SessionInvalid(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("session invalid")
        self.status = status


class SessionUnavailable(Exception):
    """The login store could not establish identity; never carries raw errors."""


class SessionPrincipalClient(Protocol):
    async def resolve(self, token: str) -> SessionPrincipal: ...


class UserLoginTokenSource(Protocol):
    async def current_token(self, uid: str, *, expected_tenant_id: str) -> str: ...


class LoginStateStore(Protocol):
    async def get(self, key: str) -> str | bytes | None: ...


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise SessionInvalid(401)
    if any(ord(character) < 32 for character in value):
        raise SessionInvalid(401)
    return value.strip()


def _decode(raw: str | bytes) -> object:
    if len(raw) > _MAX_DOCUMENT:
        raise SessionInvalid(401)
    try:
        value: object = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        for _ in range(2):
            if not isinstance(value, str):
                break
            value = json.loads(value)
        return value
    except (UnicodeError, ValueError, RecursionError):
        raise SessionInvalid(401) from None


class ErpSessionPrincipalResolver:
    """Verify JWT and the current/refresh-grace token without extending Redis TTL."""

    def __init__(
        self, store: LoginStateStore, *, jwt_secret: str, timeout_seconds: float = 2.0
    ) -> None:
        if not jwt_secret or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("ERP session settings are invalid")
        self._store = store
        self._secret = jwt_secret
        self._timeout = timeout_seconds

    async def current_token(self, uid: str, *, expected_tenant_id: str) -> str:
        """For a trusted execution identity only; never expose or persist the token.

        Existing ERP Key APIs require login, not an arbitrary user ID. Reuse the
        same JWT/cache checks as ingress rather than introducing another auth API.
        Logout/expiry fails closed; this does not extend the user's session.
        """
        uid = _identity(uid)
        expected_tenant_id = _identity(expected_tenant_id)
        try:
            async with asyncio.timeout(self._timeout):
                raw = await self._store.get(f"_Cache_Login_User_Data_{uid}")
                state = _decode(raw) if raw is not None else None
                if not isinstance(state, dict):
                    raise SessionInvalid(401)
                token: object = state.get("token")
                if not isinstance(token, str):
                    raise SessionInvalid(401)
                principal = await self.resolve(token)
                if principal.actor_id != uid or principal.tenant_id != expected_tenant_id:
                    raise SessionInvalid(403)
                return token
        except (RedisError, TimeoutError, OSError):
            raise SessionUnavailable() from None

    async def resolve(self, token: str) -> SessionPrincipal:
        if not token or len(token) > _MAX_TOKEN or not token.isascii():
            raise SessionInvalid(401)
        try:
            # ERP owns the legacy signing material; do not silently rotate or embed it here.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureKeyLengthWarning)
                payload = jwt.decode(
                    token, self._secret, algorithms=["HS256"], options={"require": ["exp"]}
                )
        except (jwt.PyJWTError, TypeError, ValueError, OverflowError):
            raise SessionInvalid(401) from None
        claims = payload.get("KEY")
        if not isinstance(claims, dict):
            raise SessionInvalid(401)
        uid = _identity(claims.get("USER"))
        if str(claims.get("MCP", "0")).lower() in {"1", "true"}:
            raise SessionInvalid(403)
        try:
            async with asyncio.timeout(self._timeout):
                return await self._resolve_login(token, uid)
        except (RedisError, TimeoutError, OSError):
            raise SessionUnavailable() from None

    async def _resolve_login(self, token: str, uid: str) -> SessionPrincipal:
        raw = await self._store.get(f"_Cache_Login_User_Data_{uid}")
        if raw is None:
            raise SessionInvalid(401)
        state = _decode(raw)
        if not isinstance(state, dict):
            raise SessionInvalid(401)
        if state.get("isMcp") is True:
            raise SessionInvalid(403)
        if state.get("isMcp") not in (False, None) or _identity(state.get("id")) != uid:
            raise SessionInvalid(401)
        current = state.get("token")
        if (
            not isinstance(current, str)
            or not current.isascii()
            or not hmac.compare_digest(token, current)
        ):
            previous: object = await self._store.get(f"_Cache_Old_Token_{uid}")
            if isinstance(previous, bytes):
                try:
                    previous = previous.decode("utf-8")
                except UnicodeError:
                    raise SessionInvalid(401) from None
            # Redis may JSON-encode a string token once; never invent a grace period.
            if isinstance(previous, str) and previous.startswith('"'):
                previous = _decode(previous)
            if (
                not isinstance(previous, str)
                or not previous.isascii()
                or not hmac.compare_digest(token, previous)
            ):
                raise SessionInvalid(401)
        try:
            cid = _identity(state.get("company"))
        except SessionInvalid:
            raise SessionInvalid(403) from None
        raw_roles = state.get("role")
        roles = (
            tuple(
                dict.fromkeys(
                    role for role in raw_roles if isinstance(role, str) and _ROLE.fullmatch(role)
                )
            )
            if isinstance(raw_roles, list)
            else ()
        )
        return SessionPrincipal(tenant_id=cid, actor_id=uid, roles=roles)
