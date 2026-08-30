"""Seed one Skill through Runtime's governed payload and evidence public seams."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_runtime.cli.application import build_payload_store
from agent_runtime.cli.configuration import DeploymentConfig
from agent_runtime.db.unit_of_work import UnitOfWork
from agent_runtime.governance.evidence import PostgresEvidenceStore
from agent_runtime.payloads.contracts import PayloadStore, PayloadWriteResult
from agent_runtime.registry.capabilities import PostgresCapabilityRegistry
from ebiz_runtime_contracts import EvidenceRef, VersionedRef
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from workflow_runtime.compiler.contracts import CapabilityDescriptor, CapabilityNotPublishedError

FIXTURE_CAPABILITY_CODE = "deployment.fixture.seed_governed_artifact"
FIXTURE_CAPABILITY_VERSION = 1
PAYLOAD_PERMISSION = "runtime:payload:read"
MAX_SKILL_BYTES = 1024 * 1024


class LocalSkillSeedError(RuntimeError):
    """A stable, secret-free deterministic seed failure."""


class CapabilityRegistry(Protocol):
    async def resolve_published(
        self, tenant_id: str, code: str, version: int
    ) -> CapabilityDescriptor: ...


class EvidenceStore(Protocol):
    async def put(self, evidence: EvidenceRef) -> EvidenceRef: ...


def _read_skill(path: Path, expected_sha256: str) -> JsonValue:
    try:
        raw = path.read_bytes()
    except OSError:
        raise LocalSkillSeedError("Skill input is not readable") from None
    if not raw or len(raw) > MAX_SKILL_BYTES:
        raise LocalSkillSeedError("Skill input exceeds its governed size boundary")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise LocalSkillSeedError("Skill input digest does not match the generated asset")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LocalSkillSeedError("Skill input must be UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise LocalSkillSeedError("Skill input must be a JSON object")
    return value


def _stable_evidence_id(tenant_id: str, payload_hash: str) -> UUID:
    identity = (
        f"ebizhub://deployment/local-evidence/{tenant_id}/"
        f"{FIXTURE_CAPABILITY_CODE}/v{FIXTURE_CAPABILITY_VERSION}/{payload_hash}"
    )
    return uuid5(NAMESPACE_URL, identity)


async def seed_governed_skill(
    *,
    app_env: str,
    local_dev_e2e: bool,
    tenant_id: str,
    skill_path: Path,
    expected_sha256: str,
    captured_at: datetime,
    registry: CapabilityRegistry,
    payload_store: PayloadStore,
    evidence_store: EvidenceStore,
) -> UUID:
    """Persist canonical Skill bytes and immutable lineage, returning only its UUID."""

    if app_env != "local_dev" or not local_dev_e2e:
        raise LocalSkillSeedError("Skill fixture requires APP_ENV=local_dev and LOCAL_DEV_E2E=true")
    if not tenant_id.strip():
        raise LocalSkillSeedError("tenant_id is required")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise LocalSkillSeedError("captured_at must be timezone-aware")
    payload = _read_skill(skill_path, expected_sha256)
    try:
        descriptor = await registry.resolve_published(
            tenant_id, FIXTURE_CAPABILITY_CODE, FIXTURE_CAPABILITY_VERSION
        )
    except CapabilityNotPublishedError:
        raise LocalSkillSeedError("local fixture capability must be published first") from None
    if descriptor.code != FIXTURE_CAPABILITY_CODE or descriptor.version != 1:
        raise LocalSkillSeedError("published local fixture capability identity is invalid")

    written = await payload_store.put_exact_restricted(
        tenant_id=tenant_id,
        payload=payload,
        required_permission=PAYLOAD_PERMISSION,
    )
    _require_external_payload(written)
    assert written.payload_ref is not None
    await payload_store.ensure_committed(
        tenant_id=tenant_id,
        payload_ref=written.payload_ref,
        payload_hash=written.payload_hash,
        size_bytes=written.size_bytes,
        content_type=written.content_type,
        classification=written.classification,
        required_permission=written.required_permission,
    )
    evidence_id = _stable_evidence_id(tenant_id, written.payload_hash)
    evidence = EvidenceRef(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        source_type="POLICY",
        source_system="deployment-local-evidence-fixture",
        external_object_id=written.payload_hash,
        captured_at=captured_at.astimezone(UTC),
        freshness_at=captured_at.astimezone(UTC),
        content_ref=written.payload_ref,
        content_hash=written.payload_hash,
        content_type=written.content_type,
        size_bytes=written.size_bytes,
        access_level=PAYLOAD_PERMISSION,
        retention_policy="LOCAL_DEV_E2E_ONLY",
        schema_version=4,
        capability_ref=VersionedRef(
            resource_type="capability",
            resource_id=descriptor.code,
            version=descriptor.version,
            digest=descriptor.content_digest,
        ),
        trace_id=f"local-skill-seed-{evidence_id}",
    )
    stored = await evidence_store.put(evidence)
    if stored.tenant_id != tenant_id or stored.evidence_id != evidence_id:
        raise LocalSkillSeedError("governed evidence store returned a tenant or identity conflict")
    if stored.content_hash != written.payload_hash:
        raise LocalSkillSeedError("governed evidence store returned an integrity conflict")
    return evidence_id


def _require_external_payload(written: PayloadWriteResult) -> None:
    if written.payload_ref is None or written.inline_payload is not None:
        raise LocalSkillSeedError("Skill payload must use governed external storage")
    if written.required_permission != PAYLOAD_PERMISSION:
        raise LocalSkillSeedError("Skill payload permission does not match Runtime artifact policy")


async def _run(arguments: argparse.Namespace) -> UUID:
    config = DeploymentConfig.from_environment()
    app_env = config.app.env
    local_dev_e2e = os.environ.get("LOCAL_DEV_E2E", "").lower() == "true"
    engine = create_async_engine(config.app.database_url.get_secret_value())
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        def unit_of_work_factory() -> UnitOfWork:
            return UnitOfWork(session_factory)

        payload_store = build_payload_store(config)
        registry = PostgresCapabilityRegistry(unit_of_work_factory)
        evidence_store = PostgresEvidenceStore(unit_of_work_factory, payload_store=payload_store)
        return await seed_governed_skill(
            app_env=app_env,
            local_dev_e2e=local_dev_e2e,
            tenant_id=arguments.tenant_id,
            skill_path=arguments.skill_file,
            expected_sha256=arguments.expected_sha256,
            captured_at=datetime.fromisoformat(arguments.captured_at.replace("Z", "+00:00")),
            registry=registry,
            payload_store=payload_store,
            evidence_store=evidence_store,
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed deterministic-local governed Skill evidence")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--skill-file", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--captured-at", required=True)
    arguments = parser.parse_args(argv)
    try:
        evidence_id = asyncio.run(_run(arguments))
    except (LocalSkillSeedError, ValueError):
        print("LOCAL_SKILL_SEED_FAILED", file=sys.stderr)
        return 1
    print(str(evidence_id))
    return 0


__all__ = [
    "FIXTURE_CAPABILITY_CODE",
    "LocalSkillSeedError",
    "main",
    "seed_governed_skill",
]
