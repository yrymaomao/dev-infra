"""Workbench session -> BFF JWT exchange for the CRM profile (decision D-1).

The browser holds an ERP session (``_token_``). It never becomes a Runtime or
Gateway credential: the BFF asks crm-service's read-only route
``GET /ai/read/v1/crm/session-principal`` who that session is (tenant, actor,
roles - resolved by crm-service's ``ErpSessionResolver``, never by a
browser-supplied field), refuses anything outside this deployment's tenant
before touching any conversation or result, and mints the same 120 s HS256 JWT
shape the conversation routes already verify (``aud=agent-runtime``,
``sub=actorId``, ``tenant_id``, ``session_key``, ``jti``).

A 401 or 403 from crm-service is passed through with the same status and the
fixed detail ``session invalid`` - no upstream body, no reason (a machine
session is a 403, a missing or expired token a 401).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import APIRouter, Cookie, Header, HTTPException

from ebiz_deployment.openclaw_reception.profile import ReceptionProfile

SESSION_PRINCIPAL_PATH = "/ai/read/v1/crm/session-principal"
TOKEN_NAME = "_token_"
_DETAIL = "session invalid"
_MAX_TOKEN = 4096
_MAX_TEXT = 256


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    tenant_id: str
    actor_id: str
    roles: tuple[str, ...]


class SessionInvalid(Exception):
    """crm-service refused the session: 401 (no/expired token) or 403 (not a human)."""

    def __init__(self, status: int) -> None:
        super().__init__(_DETAIL)
        self.status = status


class SessionUnavailable(Exception):
    """crm-service did not give a trustworthy answer."""


class SessionPrincipalClient(Protocol):
    async def resolve(self, token: str) -> SessionPrincipal: ...


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise SessionUnavailable()
    return value


class CrmSessionPrincipalClient:
    """The real client: one GET with the session as the ``_token_`` header."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("crm-service base URL must be a credential-free HTTP(S) origin")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("crm-service base URL requires HTTPS outside loopback")
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout_seconds

    async def resolve(self, token: str) -> SessionPrincipal:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    self._base_url + SESSION_PRINCIPAL_PATH,
                    headers={TOKEN_NAME: token, "Accept": "application/json"},
                )
        except httpx.HTTPError:
            raise SessionUnavailable() from None
        if response.status_code in (401, 403):
            raise SessionInvalid(response.status_code)
        if response.status_code != 200:
            raise SessionUnavailable()
        try:
            body = response.json()
        except ValueError:
            raise SessionUnavailable() from None
        if not isinstance(body, dict) or not {"tenantId", "actorId", "roles"} <= set(body):
            raise SessionUnavailable()
        roles = body["roles"]
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise SessionUnavailable()
        return SessionPrincipal(
            tenant_id=_text(body["tenantId"]),
            actor_id=_text(body["actorId"]),
            roles=tuple(roles),
        )


@dataclass(frozen=True, slots=True)
class WorkbenchTokenIssuer:
    """Mints the BFF JWT the conversation routes verify (``principal()`` in app.py)."""

    secret: str
    issuer: str = "ebizhub-crm-bff"
    audience: str = "agent-runtime"
    ttl_seconds: int = 120

    def __post_init__(self) -> None:
        if len(self.secret) < 32 or not self.issuer or self.ttl_seconds <= 0:
            raise ValueError("workbench token issuer material is invalid")

    def issue(self, profile: ReceptionProfile, principal: SessionPrincipal) -> tuple[str, int]:
        now = int(datetime.now(UTC).timestamp())
        jti = secrets.token_hex(16)
        digest = hashlib.sha256(
            f"{principal.tenant_id}\x1f{principal.actor_id}\x1f{jti}".encode()
        ).hexdigest()
        expires = now + self.ttl_seconds
        token = jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": principal.actor_id,
                "tenant_id": principal.tenant_id,
                "session_key": f"workbench:{profile.agent_id}:{digest[:48]}",
                "iat": now,
                "exp": expires,
                "jti": jti,
            },
            self.secret,
            algorithm="HS256",
        )
        return token, expires


def session_router(
    *,
    profile: ReceptionProfile,
    enabled: bool,
    tenant_id: str,
    client: SessionPrincipalClient,
    tokens: WorkbenchTokenIssuer,
) -> APIRouter:
    router = APIRouter(prefix=profile.api_prefix)

    @router.post("/session")
    async def exchange_session(
        header_token: str | None = Header(default=None, alias=TOKEN_NAME),
        cookie_token: str | None = Cookie(default=None, alias=TOKEN_NAME),
    ) -> dict[str, Any]:
        if not enabled:
            raise HTTPException(404, "CRM reception is disabled")
        token = (header_token or cookie_token or "").strip()
        if not token or len(token) > _MAX_TOKEN:
            raise HTTPException(401, _DETAIL)
        try:
            principal = await client.resolve(token)
        except SessionInvalid as error:
            raise HTTPException(error.status, _DETAIL) from None
        except SessionUnavailable:
            raise HTTPException(503, "session service unavailable") from None
        # Cross-tenant sessions are refused here, before any conversation or
        # result of this deployment's tenant can be addressed (R1-09).
        if principal.tenant_id != tenant_id:
            raise HTTPException(403, _DETAIL)
        issued, expires = tokens.issue(profile, principal)
        return {
            "jwt": issued,
            "expiresAt": expires * 1000,
            "principal": {
                "tenant_id": principal.tenant_id,
                "actor_id": principal.actor_id,
                "roles": list(principal.roles),
            },
        }

    return router


__all__ = [
    "SESSION_PRINCIPAL_PATH",
    "TOKEN_NAME",
    "CrmSessionPrincipalClient",
    "SessionInvalid",
    "SessionPrincipal",
    "SessionPrincipalClient",
    "SessionUnavailable",
    "WorkbenchTokenIssuer",
    "session_router",
]
