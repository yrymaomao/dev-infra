"""Fail-closed Supply Chain v4 release and development-smoke inputs.

Capability publication and Agent publication deliberately remain Runtime
responsibilities. This module validates operator input and verifies the
terminal public result; it does not construct Runtime internals or publish
directly to the Runtime database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from time import monotonic
from uuid import UUID

import httpx
import jwt
import yaml
from jsonschema import Draft202012Validator

_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MARKET_SCOPE = "NA_COMPANY"
_SCOPES = (
    "inventory.read",
    "sales_profit.read",
    "supply_chain.preview",
    "workflow:read",
    "workflow:start",
)
_START_RETRYABLE_ERROR = "RUNTIME_PAYLOAD_FINALIZATION_PENDING"
_START_MAX_ATTEMPTS = 8
_START_RETRY_DELAY_SECONDS = 0.25
_START_MAX_RETRY_AFTER_SECONDS = 2.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LiveSmokeSettings:
    """Explicit non-production inputs for a Runtime-hosted v4 smoke."""

    api_url: str
    jwt_secret: str = field(repr=False)
    tenant_id: str
    credential_ref: str = field(repr=False)
    sku: str
    skill_input_ref: str
    snapshot_time: str
    expected_evidence_count: int
    expected_result_status: str
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
        skill_input_ref = _required(environ, "SUPPLY_CHAIN_SKILL_INPUT_REF")
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
        expected_result_status = _required(environ, "SUPPLY_CHAIN_EXPECTED_RESULT_STATUS")
        if expected_result_status not in {"COMPLETE", "BLOCKED"}:
            raise ValueError("SUPPLY_CHAIN_EXPECTED_RESULT_STATUS must be COMPLETE or BLOCKED")
        return cls(
            api_url=api_url.rstrip("/"),
            jwt_secret=jwt_secret,
            tenant_id=tenant_id,
            credential_ref=credential_ref,
            sku=sku,
            skill_input_ref=skill_input_ref,
            snapshot_time=snapshot_time,
            expected_evidence_count=evidence_count,
            expected_result_status=expected_result_status,
            local_dev_e2e=True,
            run_id=_required(environ, "SUPPLY_CHAIN_RUN_ID"),
        )

    @property
    def inputs(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "market_scope": self.market_scope,
            "sku": self.sku,
            "snapshot_time": self.snapshot_time,
            "skill_input_ref": self.skill_input_ref,
            "fulfillment_mode": "AUTO",
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


def verify_terminal_result(
    snapshot: Mapping[str, object],
    *,
    expected_evidence_count: int,
    expected_result_status: str,
) -> str:
    """Validate one schema-4 public result and its materialized evidence count."""

    if snapshot.get("status") != "SUCCEEDED":
        raise ValueError("v4 smoke did not reach a successful terminal result")
    outputs = snapshot.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != {"result"}:
        raise ValueError("v4 smoke omitted the Runtime terminal result")
    result = outputs["result"]
    if not isinstance(result, dict):
        raise ValueError("v4 smoke omitted the Runtime terminal result")
    errors = sorted(_result_validator().iter_errors(result), key=lambda item: list(item.path))
    if errors:
        raise ValueError("v4 smoke returned a result that violates the public contract")
    evidence = result["evidence"]
    if not isinstance(evidence, list) or len(evidence) != expected_evidence_count:
        raise ValueError("v4 smoke returned the wrong materialized evidence count")
    result_status = result.get("status")
    if result_status != expected_result_status:
        raise ValueError("v4 smoke did not return the expected result status")
    if "complete_result" in outputs or "blocked_result" in outputs:
        raise ValueError("v4 smoke returned legacy branch projections")
    return str(result_status)


def _result_validator() -> Draft202012Validator:
    distribution = metadata.distribution("ebiz-agent-inventory-supply-chain")
    schema_path = Path(
        str(distribution.locate_file("inventory_supply_chain_agent/schemas/result.schema.yaml"))
    )
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise ValueError("Supply Chain result schema is unavailable")
    return Draft202012Validator(schema)


async def run_runtime_smoke(
    settings: LiveSmokeSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    """Start the published Agent through Runtime and verify its terminal result."""

    token = issue_smoke_token(
        tenant_id=settings.tenant_id,
        credential_ref=settings.credential_ref,
        secret=settings.jwt_secret,
    )
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(
        base_url=settings.api_url,
        headers=headers,
        timeout=timeout,
        transport=transport,
    ) as client:
        start_payload = {
            "agent": {"id": settings.agent_id, "version": settings.agent_version},
            "workflow": {"code": settings.workflow_code, "version": settings.workflow_version},
            "inputs": settings.inputs,
            "idempotency_key": settings.run_id,
        }
        response = await _start_execution(client, start_payload)
        snapshot = response.json()
        execution_id = snapshot.get("execution_id")
        if not isinstance(execution_id, str):
            raise ValueError("v4 smoke omitted the Runtime execution id")
        deadline = monotonic() + 120.0
        while snapshot.get("status") not in {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"}:
            if monotonic() >= deadline:
                raise ValueError("v4 smoke timed out")
            await asyncio.sleep(0.25)
            response = await client.get(f"/v1/executions/{execution_id}")
            response.raise_for_status()
            snapshot = response.json()
    result_status = verify_terminal_result(
        snapshot,
        expected_evidence_count=settings.expected_evidence_count,
        expected_result_status=settings.expected_result_status,
    )
    return {
        "execution_id": execution_id,
        "result_status": result_status,
        **settings.call_provenance,
    }


async def _start_execution(
    client: httpx.AsyncClient, payload: Mapping[str, object]
) -> httpx.Response:
    """Retry only Runtime's committed-session payload-finalization handshake."""

    for attempt in range(_START_MAX_ATTEMPTS):
        response = await client.post("/v1/agent-executions", json=payload)
        if response.status_code != 503:
            response.raise_for_status()
            return response
        try:
            detail = response.json()
        except ValueError:
            response.raise_for_status()
            raise AssertionError("unreachable")
        if (
            not isinstance(detail, dict)
            or detail.get("error_code") != _START_RETRYABLE_ERROR
            or detail.get("retryable") is not True
        ):
            response.raise_for_status()
        if attempt + 1 == _START_MAX_ATTEMPTS:
            response.raise_for_status()
        delay = _start_retry_delay(response)
        logger.warning(
            "Runtime payload finalization pending; retrying Agent start (%d/%d)",
            attempt + 1,
            _START_MAX_ATTEMPTS,
        )
        await asyncio.sleep(delay)
    raise AssertionError("bounded start retry loop exhausted without a response")


def _start_retry_delay(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return _START_RETRY_DELAY_SECONDS
    try:
        delay = float(raw)
    except ValueError:
        raise ValueError("Runtime returned an invalid Retry-After value") from None
    if delay < 0 or delay > _START_MAX_RETRY_AFTER_SECONDS:
        raise ValueError("Runtime Retry-After is outside the bounded smoke policy")
    return delay


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
        result = asyncio.run(run_runtime_smoke(settings))
    except (ValueError, httpx.HTTPError):
        print("Supply Chain v4 local smoke configuration failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


__all__ = [
    "LiveSmokeSettings",
    "issue_smoke_token",
    "main",
    "run_runtime_smoke",
    "verify_terminal_result",
]
