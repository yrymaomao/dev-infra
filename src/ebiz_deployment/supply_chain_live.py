"""Fail-closed Supply Chain v4 release and development-smoke inputs.

Capability publication and Agent publication deliberately remain Runtime
responsibilities. This module validates operator input and verifies the
terminal public result; it does not construct Runtime internals or publish
directly to the Runtime database.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MARKET_SCOPE = "NA_COMPANY"
_SCOPES = (
    "inventory.read",
    "sales_profit.read",
    "supply_chain.preview",
    "workflow:read",
    "workflow:start",
)


@dataclass(frozen=True, slots=True)
class LiveSmokeSettings:
    """Explicit non-production inputs for a Runtime-hosted v4 smoke."""

    api_url: str
    jwt_secret: str = field(repr=False)
    tenant_id: str
    credential_ref: str = field(repr=False)
    sku: str
    snapshot_time: str
    expected_evidence_count: int
    local_dev_e2e: bool
    run_id: str
    market_scope: str = _MARKET_SCOPE
    agent_id: str = "inventory-supply-chain"
    agent_version: int = 4
    workflow_code: str = "inventory-supply-chain-daily"
    workflow_version: int = 4

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> LiveSmokeSettings:
        api_url = _required(environ, "SUPPLY_CHAIN_SMOKE_API_URL")
        tenant_id = _required(environ, "SUPPLY_CHAIN_TENANT_ID")
        credential_ref = _required(environ, "SUPPLY_CHAIN_CREDENTIAL_REF")
        sku = _required(environ, "SUPPLY_CHAIN_SKU")
        snapshot_time = _required(environ, "SUPPLY_CHAIN_SNAPSHOT_TIME")
        jwt_secret = _required(environ, "APP_JWT_SECRET")
        market_scope = _required(environ, "SUPPLY_CHAIN_MARKET_SCOPE")
        if market_scope != _MARKET_SCOPE:
            raise ValueError("SUPPLY_CHAIN_MARKET_SCOPE must be NA_COMPANY for v4")
        if len(jwt_secret) < 32:
            raise ValueError("APP_JWT_SECRET must contain at least 32 characters")
        if not _CREDENTIAL_REF.fullmatch(credential_ref):
            raise ValueError("SUPPLY_CHAIN_CREDENTIAL_REF must be an opaque bounded reference")
        if not api_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
            raise ValueError("SUPPLY_CHAIN_SMOKE_API_URL must be loopback HTTP or HTTPS")
        raw_local = environ.get("LOCAL_DEV_E2E", "false").strip().lower()
        if raw_local not in {"true", "false"}:
            raise ValueError("LOCAL_DEV_E2E must be true or false")
        if raw_local != "true":
            raise ValueError("real-dev/UAT smoke is not enabled by this release")
        try:
            evidence_count = int(_required(environ, "SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT"))
        except ValueError:
            raise ValueError("SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT must be an integer") from None
        if evidence_count < 1 or evidence_count > 64:
            raise ValueError("SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT must be between 1 and 64")
        return cls(
            api_url=api_url.rstrip("/"),
            jwt_secret=jwt_secret,
            tenant_id=tenant_id,
            credential_ref=credential_ref,
            sku=sku,
            snapshot_time=snapshot_time,
            expected_evidence_count=evidence_count,
            local_dev_e2e=True,
            run_id=_required(environ, "SUPPLY_CHAIN_RUN_ID"),
        )

    @property
    def inputs(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "scope": {
                "market_scope": self.market_scope,
                "sku": self.sku,
                "snapshot_time": self.snapshot_time,
                "fulfillment_mode": "AUTO",
            },
        }

    @property
    def call_provenance(self) -> dict[str, bool]:
        return {
            "local_dev_e2e": True,
            "real_erp_calls": False,
            "production_model_calls": False,
            "production_e2e_verified": False,
        }


def issue_smoke_token(
    *, tenant_id: str, credential_ref: str, secret: str, now: datetime | None = None
) -> str:
    """Issue a short-lived local operator token with only read/preview scopes."""

    if not tenant_id or len(tenant_id) > 256:
        raise ValueError("tenant_id must be bounded and non-empty")
    if not _CREDENTIAL_REF.fullmatch(credential_ref):
        raise ValueError("credential_ref must be an opaque bounded reference")
    if len(secret) < 32:
        raise ValueError("APP_JWT_SECRET must contain at least 32 characters")
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return str(
        jwt.encode(
            {
                "aud": "agent-runtime",
                "sub": str(UUID(int=0x5C)),
                "tenant_id": tenant_id,
                "actor_type": "local-dev-smoke",
                "scopes": list(_SCOPES),
                "roles": ["platform-operator"],
                "credential_ref": credential_ref,
                "iat": issued_at,
                "exp": issued_at + timedelta(minutes=10),
            },
            secret,
            algorithm="HS256",
        )
    )


def verify_terminal_result(snapshot: Mapping[str, object]) -> str:
    """Accept one schema-4 terminal result and reject partial/legacy outputs."""

    if snapshot.get("status") != "SUCCEEDED":
        raise ValueError("v4 smoke did not reach a successful terminal result")
    result = snapshot.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("v4 smoke omitted the Runtime terminal result")
    result_status = result.get("status")
    if result_status not in {"COMPLETE", "BLOCKED"}:
        raise ValueError("v4 smoke returned an invalid terminal result")
    if "complete_result" in snapshot or "blocked_result" in snapshot:
        raise ValueError("v4 smoke returned legacy branch projections")
    return str(result_status)


def main(argv: list[str] | None = None) -> int:
    """Validate local smoke inputs; execution remains the Runtime API's job."""

    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print(
            "ebiz-supply-chain-live-smoke accepts environment configuration only", file=sys.stderr
        )
        return 2
    try:
        settings = LiveSmokeSettings.from_environment(os.environ)
    except ValueError:
        print("Supply Chain v4 local smoke configuration failed", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "agent": f"{settings.agent_id}@{settings.agent_version}",
                "workflow": f"{settings.workflow_code}@{settings.workflow_version}",
                "run_id": settings.run_id,
                **settings.call_provenance,
                "ready_for_runtime_api": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


__all__ = ["LiveSmokeSettings", "issue_smoke_token", "main", "verify_terminal_result"]
