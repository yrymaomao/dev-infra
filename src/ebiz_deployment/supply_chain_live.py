"""Deployment-owned live smoke support for the Supply Chain Agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from importlib.metadata import version as distribution_version
from time import monotonic
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import yaml
from agent_runtime.cli.smoke_read import SmokeConfig, SmokeFailure, run_smoke
from agent_runtime.db.models import (
    CapabilityDefinition,
    CapabilityVersion,
    EvidenceRefRecord,
    NodeExecution,
    SessionEventRecord,
    WorkflowExecution,
    WorkflowNode,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SMOKE_SCOPES = (
    "inventory.read",
    "replenishment.preview",
    "sales_profit.read",
    "supply_chain.compute",
    "supply_chain.preview",
    "supply_chain.skill.read",
    "workflow:author",
    "workflow:publish",
    "workflow:read",
    "workflow:start",
)
_MARKETPLACES = frozenset({"US", "CA", "MX", "UK", "DE", "FR", "IT", "ES", "JP", "AU"})


@dataclass(frozen=True, slots=True)
class CapabilityPublication:
    """One exact Runtime Capability Registry publication."""

    code: str
    version: int
    name: str
    owner: str
    risk_level: str
    effect: str
    provider_id: str
    provider_version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: tuple[str, ...]
    timeout_ms: int
    retry_limit: int
    business_idempotency_required: bool
    evidence_required: bool
    content_digest: str


@dataclass(frozen=True, slots=True)
class PublicationReport:
    """Count newly published and already verified immutable versions."""

    created: int
    verified: int


@dataclass(frozen=True, slots=True)
class LiveSmokeSettings:
    """Explicit real-system inputs for one Supply Chain Runtime smoke."""

    api_url: str
    database_url: str = field(repr=False)
    jwt_secret: str = field(repr=False)
    tenant_id: str
    credential_ref: str = field(repr=False)
    marketplace: str
    sku: str
    skill_input_ref: str
    snapshot_time: str
    expected_evidence_count: int
    erp_package_version: str
    local_dev_e2e: bool
    run_id: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> LiveSmokeSettings:
        credential_ref = _required_environment(environ, "SUPPLY_CHAIN_CREDENTIAL_REF")
        api_url = _required_environment(environ, "SUPPLY_CHAIN_SMOKE_API_URL")
        database_url = _required_environment(environ, "APP_DATABASE_URL")
        jwt_secret = _required_environment(environ, "APP_JWT_SECRET")
        tenant_id = _required_environment(environ, "SUPPLY_CHAIN_TENANT_ID")
        marketplace = _required_environment(environ, "SUPPLY_CHAIN_MARKETPLACE")
        sku = _required_environment(environ, "SUPPLY_CHAIN_SKU")
        skill_input_ref = _required_environment(environ, "SUPPLY_CHAIN_SKILL_INPUT_REF")
        snapshot_time = _required_environment(environ, "SUPPLY_CHAIN_SNAPSHOT_TIME")
        erp_version = _required_environment(environ, "ERP_PACKAGE_VERSION")
        raw_local_dev_e2e = environ.get("LOCAL_DEV_E2E", "false").lower()
        if raw_local_dev_e2e not in {"true", "false"}:
            raise ValueError("LOCAL_DEV_E2E must be true or false")
        raw_evidence_count = _required_environment(
            environ, "SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT"
        )
        try:
            evidence_count = int(raw_evidence_count)
        except ValueError:
            raise ValueError("SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT must be an integer") from None
        if evidence_count < 1 or evidence_count > 100:
            raise ValueError("SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT must be between 1 and 100")
        if marketplace not in _MARKETPLACES:
            raise ValueError("SUPPLY_CHAIN_MARKETPLACE is unsupported")
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("APP_DATABASE_URL must use postgresql+asyncpg")
        if len(jwt_secret) < 32:
            raise ValueError("APP_JWT_SECRET must contain at least 32 characters")
        if not _CREDENTIAL_REF.fullmatch(credential_ref):
            raise ValueError("SUPPLY_CHAIN_CREDENTIAL_REF must be an opaque bounded reference")
        if not api_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
            raise ValueError("SUPPLY_CHAIN_SMOKE_API_URL must be loopback HTTP or HTTPS")
        return cls(
            api_url=api_url.rstrip("/"),
            database_url=database_url,
            jwt_secret=jwt_secret,
            tenant_id=tenant_id,
            credential_ref=credential_ref,
            marketplace=marketplace,
            sku=sku,
            skill_input_ref=skill_input_ref,
            snapshot_time=snapshot_time,
            expected_evidence_count=evidence_count,
            erp_package_version=erp_version,
            local_dev_e2e=raw_local_dev_e2e == "true",
            run_id=f"supply-chain-live-{uuid4()}",
        )

    @property
    def inputs(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "marketplace": self.marketplace,
            "sku": self.sku,
            "snapshot_time": self.snapshot_time,
            "skill_input_ref": self.skill_input_ref,
        }

    @property
    def provider_versions(self) -> dict[str, str]:
        supply_chain_version = distribution_version("ebiz-agent-inventory-supply-chain")
        return {
            provider_id: (
                self.erp_package_version if provider_id == "yeaher.erp" else supply_chain_version
            )
            for provider_id in PROVIDER_VERSION_KEYS
        }

    @property
    def call_provenance(self) -> dict[str, bool]:
        """Describe whether this smoke reached real production dependencies."""

        return {
            "local_dev_e2e": self.local_dev_e2e,
            "real_erp_calls": not self.local_dev_e2e,
            "production_model_calls": not self.local_dev_e2e,
        }


def load_capability_publications(
    *, provider_versions: Mapping[str, str]
) -> tuple[CapabilityPublication, ...]:
    """Load the installed wheel contracts and derive immutable registry rows."""

    package = resources.files("inventory_supply_chain")
    capabilities = yaml.safe_load(package.joinpath("capabilities.yaml").read_text("utf-8"))
    schema = yaml.safe_load(
        package.joinpath("schemas/supply-chain-inventory.schema.yaml").read_text("utf-8")
    )
    if not isinstance(capabilities, dict) or not isinstance(schema, dict):
        raise ValueError("Supply Chain wheel contracts are invalid")
    items = capabilities.get("capabilities")
    definitions = schema.get("$defs")
    if not isinstance(items, list) or not isinstance(definitions, dict):
        raise ValueError("Supply Chain wheel contracts are incomplete")

    publications: list[CapabilityPublication] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("Supply Chain capability entry is invalid")
        provider_id = _required_string(raw, "provider_id")
        provider_version = provider_versions.get(provider_id, "")
        if not provider_version:
            raise ValueError(f"provider version is not pinned for {provider_id}")
        input_schema = _standalone_schema(schema, _required_string(raw, "input_schema_ref"))
        output_schema = _standalone_schema(schema, _required_string(raw, "output_schema_ref"))
        retry = raw.get("retry")
        idempotency = raw.get("idempotency")
        evidence = raw.get("evidence")
        permissions = raw.get("permission_scopes")
        if (
            not isinstance(retry, dict)
            or not isinstance(idempotency, dict)
            or not isinstance(evidence, dict)
            or not isinstance(permissions, list)
            or not all(isinstance(item, str) and item for item in permissions)
        ):
            raise ValueError("Supply Chain capability governance is invalid")
        unsigned = CapabilityPublication(
            code=_required_string(raw, "capability_id"),
            version=_required_integer(raw, "version"),
            name=_required_string(raw, "purpose"),
            owner=_required_string(raw, "owner"),
            risk_level=_required_string(raw, "risk_level"),
            effect=_required_string(raw, "effect"),
            provider_id=provider_id,
            provider_version=provider_version,
            input_schema=input_schema,
            output_schema=output_schema,
            permissions=tuple(sorted(permissions)),
            timeout_ms=_required_integer(raw, "timeout_seconds") * 1000,
            retry_limit=_required_integer(retry, "max_attempts") - 1,
            business_idempotency_required=bool(idempotency.get("business_required")),
            evidence_required=bool(evidence.get("required")),
            content_digest="",
        )
        digest_payload = asdict(unsigned)
        digest_payload.pop("content_digest")
        digest = hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        publications.append(
            CapabilityPublication(**{**digest_payload, "content_digest": digest})
        )
    return tuple(publications)


def issue_smoke_token(
    *,
    tenant_id: str,
    credential_ref: str,
    secret: str,
    now: datetime | None = None,
) -> str:
    """Issue a ten-minute operator JWT bound to one opaque login credential."""

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
                "actor_type": "smoke",
                "scopes": list(_SMOKE_SCOPES),
                "roles": ["platform-operator"],
                "credential_ref": credential_ref,
                "iat": issued_at,
                "exp": issued_at + timedelta(minutes=10),
            },
            secret,
            algorithm="HS256",
        )
    )


async def publish_capabilities(
    *,
    database_url: str,
    tenant_id: str,
    provider_versions: Mapping[str, str],
) -> PublicationReport:
    """Idempotently publish the installed Supply Chain contracts to PostgreSQL."""

    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("database_url must use postgresql+asyncpg")
    if not tenant_id or len(tenant_id) > 256:
        raise ValueError("tenant_id must be bounded and non-empty")
    publications = load_capability_publications(provider_versions=provider_versions)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created = 0
    verified = 0
    try:
        async with sessions() as session:
            for item in publications:
                definition = await session.scalar(
                    select(CapabilityDefinition).where(
                        CapabilityDefinition.tenant_id == tenant_id,
                        CapabilityDefinition.code == item.code,
                    )
                )
                if definition is None:
                    definition = CapabilityDefinition(
                        tenant_id=tenant_id,
                        code=item.code,
                        name=item.name,
                        owner=item.owner,
                        risk_level=item.risk_level,
                        status="ACTIVE",
                    )
                    session.add(definition)
                    await session.flush()
                else:
                    _verify_definition(definition, item)
                version = await session.scalar(
                    select(CapabilityVersion).where(
                        CapabilityVersion.tenant_id == tenant_id,
                        CapabilityVersion.capability_definition_id == definition.id,
                        CapabilityVersion.version == item.version,
                    )
                )
                if version is None:
                    session.add(
                        CapabilityVersion(
                            id=uuid4(),
                            tenant_id=tenant_id,
                            capability_definition_id=definition.id,
                            version=item.version,
                            status="PUBLISHED",
                            effect=item.effect,
                            provider_id=item.provider_id,
                            provider_version=item.provider_version,
                            input_schema=item.input_schema,
                            output_schema=item.output_schema,
                            permissions=list(item.permissions),
                            idempotency_policy="invocation",
                            timeout_ms=item.timeout_ms,
                            retry_limit=item.retry_limit,
                            business_idempotency_required=item.business_idempotency_required,
                            evidence_required=item.evidence_required,
                            content_digest=item.content_digest,
                        )
                    )
                    created += 1
                else:
                    _verify_version(version, item)
                    verified += 1
            await session.commit()
    finally:
        await engine.dispose()
    return PublicationReport(created=created, verified=verified)


def verify_complete_snapshot(snapshot: Mapping[str, object]) -> str:
    """Require the successful, mutually exclusive Supply Chain complete branch."""

    outputs = snapshot.get("outputs")
    if snapshot.get("status") != "SUCCEEDED" or not isinstance(outputs, dict):
        raise ValueError("live smoke did not produce only complete_result")
    if set(outputs) != {"complete_result"}:
        raise ValueError("live smoke did not produce only complete_result")
    complete = outputs.get("complete_result")
    if not isinstance(complete, dict) or complete.get("status") != "COMPLETE":
        raise ValueError("live smoke did not produce only complete_result")
    return "complete_result"


def _verify_complete_branch_nodes(statuses: Mapping[str, str]) -> str:
    """Verify the mutually exclusive local branch from durable Runtime node facts."""

    complete = statuses.get("build_result") == "SUCCEEDED"
    blocked = statuses.get("build_blocked_result") == "SUCCEEDED"
    if not complete or blocked:
        raise ValueError("local smoke did not produce only complete_result")
    return "complete_result"


def _verify_durable_smoke_facts(
    *,
    status: str,
    event_types: list[str],
    evidence_count: int,
    expected_evidence_count: int,
) -> None:
    required = {
        "session.started",
        "workflow.started",
        "capability.call.started",
        "evidence.attached",
        "capability.call.completed",
        "workflow.completed",
    }
    if status != "SUCCEEDED":
        raise ValueError("local timeout recovery did not reach SUCCEEDED")
    if not required <= set(event_types):
        raise ValueError("local timeout recovery event sequence is incomplete")
    if evidence_count != expected_evidence_count:
        raise ValueError("local timeout recovery evidence count does not match")


async def _recover_local_timed_out_smoke(
    settings: LiveSmokeSettings,
) -> dict[str, object]:
    """Recover a synchronous start whose HTTP client timed out before the Runtime did."""

    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scoped_key = f"{settings.tenant_id}:{settings.run_id}"
    deadline = monotonic() + 120.0
    execution: WorkflowExecution | None = None
    try:
        while monotonic() < deadline:
            async with sessions() as session:
                execution = await session.scalar(
                    select(WorkflowExecution).where(
                        WorkflowExecution.tenant_id == settings.tenant_id,
                        WorkflowExecution.invocation_idempotency_key == scoped_key,
                    )
                )
            if execution is not None and execution.status in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "REJECTED",
            }:
                break
            await asyncio.sleep(0.25)
        if execution is None:
            raise SmokeFailure("local timed-out smoke execution was not persisted")
        async with sessions() as session:
            event_types = [
                str(item)
                for item in (
                    await session.scalars(
                        select(SessionEventRecord.event_type)
                        .where(
                            SessionEventRecord.tenant_id == settings.tenant_id,
                            SessionEventRecord.execution_id == execution.id,
                        )
                        .order_by(SessionEventRecord.sequence)
                    )
                ).all()
            ]
            evidence_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(EvidenceRefRecord)
                    .where(
                        EvidenceRefRecord.tenant_id == settings.tenant_id,
                        EvidenceRefRecord.execution_id == execution.id,
                    )
                )
                or 0
            )
        _verify_durable_smoke_facts(
            status=execution.status,
            event_types=event_types,
            evidence_count=evidence_count,
            expected_evidence_count=settings.expected_evidence_count,
        )
        return {
            "execution_id": str(execution.id),
            "status": execution.status,
            "event_count": len(event_types),
            "evidence_count": evidence_count,
            "request_timed_out_recovered": True,
        }
    finally:
        await engine.dispose()


async def _verify_local_complete_branch(
    *, database_url: str, tenant_id: str, execution_id: UUID
) -> str:
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(WorkflowNode.node_key, NodeExecution.status)
                    .join(
                        NodeExecution,
                        NodeExecution.workflow_node_id == WorkflowNode.id,
                    )
                    .where(
                        NodeExecution.tenant_id == tenant_id,
                        NodeExecution.workflow_execution_id == execution_id,
                        WorkflowNode.node_key.in_({"build_result", "build_blocked_result"}),
                    )
                )
            ).all()
        return _verify_complete_branch_nodes({str(key): str(status) for key, status in rows})
    finally:
        await engine.dispose()


async def run_live_smoke(settings: LiveSmokeSettings) -> dict[str, object]:
    """Publish contracts and execute the installed workflow through the live Runtime API."""

    publication = await publish_capabilities(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        provider_versions=settings.provider_versions,
    )
    token = issue_smoke_token(
        tenant_id=settings.tenant_id,
        credential_ref=settings.credential_ref,
        secret=settings.jwt_secret,
    )
    workflow_resource = resources.files("inventory_supply_chain").joinpath(
        "workflows/inventory-supply-chain-daily.yaml"
    )
    with resources.as_file(workflow_resource) as workflow_path:
        smoke_config = SmokeConfig(
                api_url=settings.api_url,
                token=token,
                tenant_id=settings.tenant_id,
                workflow_code="inventory-supply-chain-daily",
                workflow_version=1,
                workflow_source=workflow_path,
                inputs=settings.inputs,
                idempotency_key=settings.run_id,
                database_url=settings.database_url,
                expected_evidence_count=settings.expected_evidence_count,
                wait_seconds=120.0,
            )
        try:
            result = await run_smoke(smoke_config)
        except httpx.ReadTimeout:
            if not settings.local_dev_e2e:
                raise
            result = await _recover_local_timed_out_smoke(settings)
    execution_id = result.get("execution_id")
    if not isinstance(execution_id, str):
        raise SmokeFailure("Supply Chain smoke omitted execution_id")
    if settings.local_dev_e2e:
        branch = await _verify_local_complete_branch(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            execution_id=UUID(execution_id),
        )
    else:
        async with httpx.AsyncClient(
            base_url=settings.api_url,
            timeout=httpx.Timeout(30.0),
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            response = await client.get(f"/v1/executions/{execution_id}")
        if response.status_code != 200:
            raise SmokeFailure("Supply Chain execution verification failed")
        branch = verify_complete_snapshot(response.json())
    return {
        **result,
        "capabilities_created": publication.created,
        "capabilities_verified": publication.verified,
        "result_branch": branch,
        "public_result_projection_verified": not settings.local_dev_e2e,
        **settings.call_provenance,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the environment-configured live smoke with sanitized failure output."""

    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print(
            "ebiz-supply-chain-live-smoke accepts environment configuration only",
            file=sys.stderr,
        )
        return 2
    try:
        settings = LiveSmokeSettings.from_environment(os.environ)
        result = asyncio.run(run_live_smoke(settings))
    except (OSError, ValueError, httpx.HTTPError, SmokeFailure):
        print("Supply Chain live Runtime smoke failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def _verify_definition(
    existing: CapabilityDefinition, expected: CapabilityPublication
) -> None:
    observed = (
        existing.name,
        existing.owner,
        existing.risk_level,
        existing.status,
    )
    wanted = (expected.name, expected.owner, expected.risk_level, "ACTIVE")
    if observed != wanted:
        raise ValueError(f"published capability definition conflicts with {expected.code}")


def _verify_version(existing: CapabilityVersion, expected: CapabilityPublication) -> None:
    observed = (
        existing.status,
        existing.effect,
        existing.provider_id,
        existing.provider_version,
        dict(existing.input_schema),
        dict(existing.output_schema),
        tuple(existing.permissions),
        existing.idempotency_policy,
        existing.timeout_ms,
        existing.retry_limit,
        existing.business_idempotency_required,
        existing.evidence_required,
        existing.content_digest,
    )
    wanted = (
        "PUBLISHED",
        expected.effect,
        expected.provider_id,
        expected.provider_version,
        expected.input_schema,
        expected.output_schema,
        expected.permissions,
        "invocation",
        expected.timeout_ms,
        expected.retry_limit,
        expected.business_idempotency_required,
        expected.evidence_required,
        expected.content_digest,
    )
    if observed != wanted:
        raise ValueError(
            f"published capability version conflicts with {expected.code}@{expected.version}"
        )


def _standalone_schema(root: dict[str, Any], reference: str) -> dict[str, Any]:
    prefix = "schemas/supply-chain-inventory.schema.yaml#/$defs/"
    if not reference.startswith(prefix):
        raise ValueError("capability schema reference is outside the installed contract")
    definition = reference.removeprefix(prefix)
    definitions = root.get("$defs")
    if not isinstance(definitions, dict) or definition not in definitions:
        raise ValueError("capability schema definition is missing")
    return {
        "$schema": root.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$ref": f"#/$defs/{definition}",
        "$defs": json.loads(json.dumps(definitions)),
    }


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _required_integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValueError(f"{key} must be a positive integer")
    return item


def _required_environment(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


PROVIDER_VERSION_KEYS = MappingProxyType(
    {
        "yeaher.erp": "ERP_PACKAGE_VERSION",
        "inventory-supply-chain.scope-validator": "SUPPLY_CHAIN_PLUGIN_VERSION",
        "inventory-supply-chain.skill-loader": "SUPPLY_CHAIN_PLUGIN_VERSION",
        "inventory-supply-chain.evidence-gate": "SUPPLY_CHAIN_PLUGIN_VERSION",
        "inventory-supply-chain.forecast-engine": "SUPPLY_CHAIN_PLUGIN_VERSION",
        "inventory-supply-chain.classification-engine": "SUPPLY_CHAIN_PLUGIN_VERSION",
        "inventory-supply-chain.replenishment-engine": "SUPPLY_CHAIN_PLUGIN_VERSION",
        "inventory-supply-chain.result-builder": "SUPPLY_CHAIN_PLUGIN_VERSION",
        "inventory-supply-chain.blocked-result-builder": "SUPPLY_CHAIN_PLUGIN_VERSION",
    }
)
