"""Deployment-owned live smoke support for the Supply Chain Agent."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from importlib.metadata import version as distribution_version
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from uuid import UUID, uuid4

import httpx
import jwt
from agent_runtime.cli.smoke_read import SmokeConfig, SmokeFailure, run_smoke
from agent_runtime.db.models import (
    EvidenceRefRecord,
    NodeExecution,
    SessionEventRecord,
    WorkflowExecution,
    WorkflowNode,
)
from agent_runtime.db.unit_of_work import UnitOfWork
from agent_runtime.registry.capability_manifest import (
    CapabilityPublicationError,
    CapabilitySetPublication,
    load_capability_set,
)
from agent_runtime.registry.capability_publication import (
    CapabilityPublicationReport,
    CapabilityPublicationService,
)
from ebiz_runtime_contracts import ActorRef
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

# Deployment-owned identity for administrative capability publication. The
# actor id matches the operator subject used by the smoke token.
_PUBLICATION_ACTOR = ActorRef(actor_id=UUID(int=0x5C), actor_type="DEPLOYMENT")
_PUBLICATION_TRACE_ID = "supply-chain-live-publish"


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


def _installed_contract_root() -> Path:
    """Resolve the contract directory packaged inside the installed wheel.

    Selecting the business contract root is a deployment responsibility: the
    Runtime publisher never discovers business distributions itself, so the
    deployment layer resolves the installed ``inventory_supply_chain``
    distribution directory and hands it to the public loader explicitly.
    """

    root = Path(str(resources.files("inventory_supply_chain")))
    if not root.is_dir():
        raise ValueError("Supply Chain contracts are not installed as a real directory")
    return root


def load_supply_chain_capability_set(
    *, provider_versions: Mapping[str, str]
) -> CapabilitySetPublication:
    """Project the installed wheel contracts through the public Runtime loader."""

    root = _installed_contract_root()
    return load_capability_set(
        manifest_path=root / "capabilities.yaml",
        contract_root=root,
        provider_versions=provider_versions,
    )


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
) -> CapabilityPublicationReport:
    """Idempotently publish the installed Supply Chain contracts to PostgreSQL."""

    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("database_url must use postgresql+asyncpg")
    if not tenant_id or len(tenant_id) > 256:
        raise ValueError("tenant_id must be bounded and non-empty")
    publication = load_supply_chain_capability_set(provider_versions=provider_versions)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        service = CapabilityPublicationService(lambda: UnitOfWork(sessions))
        return await service.publish_set(
            tenant_id=tenant_id,
            publication=publication,
            actor=_PUBLICATION_ACTOR,
            trace_id=_PUBLICATION_TRACE_ID,
        )
    finally:
        await engine.dispose()


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
    except (OSError, ValueError, httpx.HTTPError, SmokeFailure, CapabilityPublicationError):
        print("Supply Chain live Runtime smoke failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


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
