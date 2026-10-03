"""Exchange verified ERP sessions for the unchanged short-lived CRM JWT."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from fastapi import APIRouter, Cookie, Header, HTTPException

from ebiz_deployment.erp_session import (
    SessionInvalid,
    SessionPrincipal,
    SessionPrincipalClient,
    SessionUnavailable,
)
from ebiz_deployment.openclaw_reception.profile import ReceptionProfile

TOKEN_NAME = "_token_"
_DETAIL = "session invalid"
_MAX_TOKEN = 4096


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
