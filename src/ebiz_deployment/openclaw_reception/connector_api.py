"""Adapter- and Gateway-facing connector routes of one reception profile.

``{api_prefix}/credentials`` and ``{api_prefix}/runs/end`` are called by the
profile's OpenClaw Adapter instance with the connector credential;
``{internal_prefix}/authorize`` is called by the Runtime Tool Gateway with the
same credential and answers the current-policy question for one run binding.
The authorize reply never names an offer outside ``profile.offer_ids`` and
never accepts a session key that another profile's agent instance minted.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException

from .connector_contracts import (
    OpenClawCredentialRequest,
    OpenClawEndRunRequest,
    OpenClawPolicyCheckRequest,
)
from .conversation_repository import ConversationRepository
from .profile import ReceptionProfile
from .run_binding import RunBindingConflict, RunBindings


@dataclass(frozen=True, slots=True)
class RunTokenIssuer:
    """Mints the short-lived run JWT the Adapter presents to the Tool Gateway."""

    key: str
    issuer: str
    audience: str
    ttl_seconds: int = 120

    def __post_init__(self) -> None:
        if len(self.key) < 32 or not self.issuer or not self.audience or self.ttl_seconds <= 0:
            raise ValueError("run token issuer material is invalid")

    def issue(self, identity: dict[str, str], now: datetime) -> tuple[str, int]:
        issued = int(now.timestamp())
        expires = issued + self.ttl_seconds
        token = jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": identity["principalId"],
                "tenant_id": identity["tenantId"],
                "session_key": identity["sessionKey"],
                "iat": issued,
                "exp": expires,
                "jti": secrets.token_hex(16),
            },
            self.key,
            algorithm="HS256",
        )
        return token, expires


@dataclass(frozen=True, slots=True)
class StaticRunIdentity:
    """A deployment-constant selector -> identity mapping (the Supply Chain dev binding)."""

    selector: str
    tenant_id: str
    principal_id: str


def connector_router(
    *,
    profile: ReceptionProfile,
    bindings: Callable[..., RunBindings],
    conversations: ConversationRepository | None,
    connector: Callable[..., None],
    tokens: RunTokenIssuer | None,
    static_identity: StaticRunIdentity | None,
) -> APIRouter:
    router = APIRouter()

    @router.post(f"{profile.api_prefix}/credentials")
    async def exchange_credential(
        body: OpenClawCredentialRequest,
        _connector: None = Depends(connector),
        repository: RunBindings = Depends(bindings),
    ) -> dict[str, object]:
        if tokens is None:
            raise HTTPException(status_code=403, detail="connector binding denied")
        model_session: str | None = None
        turn_id = None
        if body.selector.startswith(("turn:", "reconcile:")) and conversations is not None:
            try:
                tenant, user, model_session, turn_id = await conversations.credential_context(
                    body.selector,
                    body.run_id,
                )
            except (LookupError, ValueError):
                raise HTTPException(403, "connector binding denied") from None
        elif static_identity is not None and body.selector == static_identity.selector:
            tenant, user = static_identity.tenant_id, static_identity.principal_id
        else:
            raise HTTPException(403, "connector binding denied")
        now = datetime.now(UTC)
        try:
            identity = await repository.exchange_openclaw_run(
                selector=body.selector,
                run_id=body.run_id,
                tenant_id=tenant,
                principal_id=user,
                agent_id=profile.agent_id,
                now=now,
                model_session_id=model_session,
                turn_id=turn_id,
            )
        except RunBindingConflict as error:
            raise HTTPException(409, str(error)) from None
        token, expires = tokens.issue(identity, now)
        return {"identity": identity, "jwt": token, "expiresAt": expires * 1000}

    @router.post(f"{profile.api_prefix}/runs/end")
    async def end_run(
        body: OpenClawEndRunRequest,
        _connector: None = Depends(connector),
        repository: RunBindings = Depends(bindings),
    ) -> dict[str, bool]:
        ended = await repository.end_openclaw_run(run_id=body.run_id, now=datetime.now(UTC))
        return {"ended": ended}

    @router.post(f"{profile.internal_prefix}/authorize", include_in_schema=False)
    async def authorize_run(
        body: OpenClawPolicyCheckRequest,
        _connector: None = Depends(connector),
        repository: RunBindings = Depends(bindings),
    ) -> dict[str, Any]:
        active = False
        revision = "0"
        if profile.owns_session_key(body.session_key):
            active, revision = await repository.authorize_openclaw_run(
                tenant_id=body.tenant_id,
                principal_id=body.principal_id,
                session_key=body.session_key,
                now=datetime.now(UTC),
            )
        allowed = (
            [offer_id for offer_id in body.candidate_offer_ids if offer_id in profile.offer_ids]
            if active
            else []
        )
        return {
            "binding_active": active,
            "policy_revision": revision,
            "allowed_offer_ids": allowed,
        }

    return router


__all__ = ["RunTokenIssuer", "StaticRunIdentity", "connector_router"]
