"""Publish the CRM advise workflow from the installed CRM agent wheel (design §5.3).

``crm-case-advise-on-demand@1`` is not in the CRM ``agent-contract.yaml``
(Runtime 0.1.5's AgentContract carries exactly one workflow, the WRITE
orchestrate graph). It reaches the Registry through this deployment job:

1. publish the two Catalog sets the graph binds - ``crm@2`` and
   ``crm-advise@1`` - through ``CapabilityPublicationService`` (an exact replay
   of an already published set is verified, never re-emitted);
2. compile ``workflows/crm-case-advise-on-demand.yaml`` with
   ``WorkflowCompiler`` against those publications, exactly as
   ``crm-agent/tools/compile_advise_workflow.py`` does, so the IR4 checksum is
   the one that repository's tests assert;
3. ``WorkflowPublicationMaterial.from_compiled`` ->
   ``WorkflowPublicationService`` draft / validate / publish (idempotent);
4. print the published checksum and exit non-zero unless it equals
   ``--expect-digest`` - the value the deployment pins as
   ``CRM_ADVISOR_WORKFLOW_DIGEST``.

Nothing here decides anything about the graph: a WRITE binding or a write
capability node in the compiled IR refuses the publication.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import yaml
from agent_runtime.registry.capability_manifest import (
    CapabilityCatalogPublication,
    load_capability_publication,
)
from agent_runtime.registry.workflow_material import WorkflowPublicationMaterial
from agent_runtime.registry.workflow_publication import WorkflowPublicationService
from ebiz_runtime_contracts import ActorRef
from workflow_runtime.authoring.models import WorkflowDocument
from workflow_runtime.canonical.models import CanonicalWorkflowIR
from workflow_runtime.compiler.compiler import WorkflowCompiler
from workflow_runtime.compiler.contracts import (
    CapabilityDescriptor,
    CapabilityNotPublishedError,
    ModelProfileDescriptor,
    UnknownModelProfileError,
)
from workflow_runtime.registry.contracts import PublishedWorkflow, WorkflowPublicationRegistry

from .config import (
    CRM_ADVISE_WORKFLOW,
    CRM_ADVISE_WORKFLOW_VERSION,
    CRM_AGENT_DISTRIBUTION_VERSION,
    CRM_CAPABILITY_SETS,
)

AGENT_PACKAGE = "crm_agent"
CATALOG_MANIFESTS = ("capabilities.yaml", "capabilities-advise.yaml")
WORKFLOW_REF = "workflows/crm-case-advise-on-demand.yaml"
PROVIDER_VERSION = CRM_AGENT_DISTRIBUTION_VERSION
TRACE_ID = "crm-advise-publication"
_DATABASE_URL_ENV = "AGENT_RUNTIME_DATABASE_URL"
_RESOURCE_REF_KEYS = frozenset({"output_schema_ref", "schema_ref", "policy_ref"})


class CrmAdvisePublicationError(RuntimeError):
    """The publication refused; the message never carries database or provider details."""


@dataclass(frozen=True, slots=True)
class CatalogSetReceipt:
    set_id: str
    version: int
    content_digest: str
    created: int
    verified: int


@dataclass(frozen=True, slots=True)
class CrmAdvisePublicationReceipt:
    catalog_sets: tuple[CatalogSetReceipt, ...]
    workflow_code: str
    workflow_version: int
    version_id: UUID
    checksum: str
    compiler_version: str
    schema_version: int
    input_schema_digest: str | None
    result_schema_digest: str | None
    capability_pins: tuple[str, ...]
    permission_scopes: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "catalog_sets": [
                {
                    "set_id": item.set_id,
                    "version": item.version,
                    "content_digest": item.content_digest,
                    "created": item.created,
                    "verified": item.verified,
                }
                for item in self.catalog_sets
            ],
            "workflow": f"{self.workflow_code}@{self.workflow_version}",
            "version_id": str(self.version_id),
            "checksum": self.checksum,
            "CRM_ADVISOR_WORKFLOW_DIGEST": self.checksum,
            "compiler_version": self.compiler_version,
            "schema_version": self.schema_version,
            "input_schema_digest": self.input_schema_digest,
            "result_schema_digest": self.result_schema_digest,
            "capability_pins": list(self.capability_pins),
            "permission_scopes": list(self.permission_scopes),
        }


class CapabilitySetPublisher(Protocol):
    async def publish_set(
        self,
        *,
        tenant_id: str,
        publication: Any,
        actor: ActorRef,
        trace_id: str,
    ) -> Any: ...


def agent_contract_root() -> Path:
    """The installed ``crm_agent`` wheel resources (manifests, schemas, workflows)."""

    root = Path(str(files(AGENT_PACKAGE)))
    if not (root / "capabilities.yaml").is_file():
        raise CrmAdvisePublicationError("the installed ebiz-agent-crm wheel carries no manifests")
    return root


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CrmAdvisePublicationError(f"{path.name} must be a YAML mapping")
    return value


def load_catalog_publications(root: Path) -> tuple[CapabilityCatalogPublication, ...]:
    publications: list[CapabilityCatalogPublication] = []
    for name in CATALOG_MANIFESTS:
        manifest = load_yaml(root / name)
        publication = load_capability_publication(
            manifest_path=root / name,
            contract_root=root,
            provider_versions={
                item["provider_id"]: PROVIDER_VERSION for item in manifest["capabilities"]
            },
        )
        if not isinstance(publication, CapabilityCatalogPublication):
            raise CrmAdvisePublicationError(f"{name} did not load as a v2 Catalog publication")
        publications.append(publication)
    identities = tuple((item.set_id, item.version) for item in publications)
    if identities != CRM_CAPABILITY_SETS:
        raise CrmAdvisePublicationError("the CRM wheel does not carry crm@2 and crm-advise@1")
    return tuple(publications)


def _walk(node: object) -> Iterator[tuple[str, object]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def resource_bundle(root: Path, workflow: dict[str, Any]) -> dict[str, Any]:
    """Every resource the workflow reaches, transitively, and nothing else."""

    pending = {
        str(value).partition("#")[0]
        for key, value in _walk(workflow)
        if key in _RESOURCE_REF_KEYS and isinstance(value, str)
    }
    bundle: dict[str, Any] = {}
    while pending:
        path = pending.pop()
        if path in bundle:
            continue
        document = load_yaml(root / path)
        bundle[path] = document
        for key, value in _walk(document):
            if key == "$ref" and isinstance(value, str):
                file_part = value.partition("#")[0]
                if file_part:
                    pending.add((PurePosixPath(path).parent / file_part).as_posix())
        pending -= set(bundle)
    return dict(sorted(bundle.items()))


class _CompilerRegistry:
    def __init__(self, publications: tuple[CapabilityCatalogPublication, ...]) -> None:
        self._descriptors = {
            (item.code, item.version): CapabilityDescriptor(
                code=item.code,
                version=item.version,
                content_digest=item.content_digest,
                effect=cast(Literal["READ", "PREVIEW", "WRITE"], item.effect),
                input_schema=item.input_schema,
                output_schema=item.output_schema,
                permission_scopes=list(item.permissions),
                evidence_required=item.evidence_required,
                idempotency_required=True,
                approval_required=(item.effect == "WRITE"),
                retryable_error_codes=[],
                timeout_seconds_limit=max(1, item.timeout_ms // 1000),
                max_attempts_limit=max(1, item.retry_limit + 1),
                runtime_binding=item.provider_id,
                risk_level=item.risk_level,
                reversibility=item.reversibility,
                max_hosting_level=item.max_hosting_level,
                write_policy=item.write_policy,
                write_certification_status=item.write_certification_status,
            )
            for publication in publications
            for item in publication.capabilities
        }

    def resolve_published(self, code: str, version: int) -> CapabilityDescriptor:
        descriptor = self._descriptors.get((code, version))
        if descriptor is None:
            raise CapabilityNotPublishedError(code, version)
        return descriptor


class _CompilerModels:
    def require_profile(self, profile: str) -> ModelProfileDescriptor:
        if profile not in {"structured", "reasoning", "fast"}:
            raise UnknownModelProfileError(profile)
        return ModelProfileDescriptor(profile=profile, max_timeout_seconds=600)


@dataclass(frozen=True, slots=True)
class CompiledAdviseWorkflow:
    source: bytes
    resources: dict[str, Any]
    ir: CanonicalWorkflowIR


def compile_advise_workflow(
    root: Path, publications: tuple[CapabilityCatalogPublication, ...]
) -> CompiledAdviseWorkflow:
    source = (root / WORKFLOW_REF).read_bytes()
    document = load_yaml(root / WORKFLOW_REF)
    authored = WorkflowDocument.model_validate(document)
    resources = resource_bundle(root, document)
    ir = WorkflowCompiler().compile(
        authored,
        _CompilerRegistry(publications),
        _CompilerModels(),
        resources=resources,
        trace_id=TRACE_ID,
    )
    if not isinstance(ir, CanonicalWorkflowIR):
        raise CrmAdvisePublicationError("the compiler did not return a canonical IR")
    identity = (ir.workflow_identity.code, ir.workflow_identity.version)
    if identity != (CRM_ADVISE_WORKFLOW, CRM_ADVISE_WORKFLOW_VERSION):
        raise CrmAdvisePublicationError(
            "the wheel's advise workflow is not crm-case-advise-on-demand@1"
        )
    if ir.governance_policy.write_capability_nodes or any(
        binding.effect == "WRITE" for binding in ir.capability_bindings
    ):
        raise CrmAdvisePublicationError("REFUSED: the advise workflow binds a WRITE capability")
    if ir.checksum is None:
        raise CrmAdvisePublicationError("the advise IR carries no checksum")
    return CompiledAdviseWorkflow(source=source, resources=resources, ir=ir)


async def publish_crm_advise(
    *,
    capabilities: CapabilitySetPublisher,
    workflows: WorkflowPublicationRegistry,
    tenant_id: str,
    actor: ActorRef,
    trace_id: str = TRACE_ID,
    root: Path | None = None,
) -> CrmAdvisePublicationReceipt:
    """Catalog sets first, then the compiled workflow; every step is an idempotent replay."""

    contract_root = root or agent_contract_root()
    publications = load_catalog_publications(contract_root)
    receipts = []
    for publication in publications:
        report = await capabilities.publish_set(
            tenant_id=tenant_id, publication=publication, actor=actor, trace_id=trace_id
        )
        receipts.append(
            CatalogSetReceipt(
                set_id=publication.set_id,
                version=publication.version,
                content_digest=publication.content_digest,
                created=int(report.created),
                verified=int(report.verified),
            )
        )
    compiled = compile_advise_workflow(contract_root, publications)
    material = WorkflowPublicationMaterial.from_compiled(
        source=compiled.source, resources=compiled.resources, ir=compiled.ir
    )
    published: PublishedWorkflow = await WorkflowPublicationService(workflows).ensure_published(
        material=material, tenant_id=tenant_id, actor=actor, trace_id=trace_id
    )
    if published.checksum != compiled.ir.checksum:
        raise CrmAdvisePublicationError("the Registry published a different advise checksum")
    ir = compiled.ir
    return CrmAdvisePublicationReceipt(
        catalog_sets=tuple(receipts),
        workflow_code=published.code,
        workflow_version=published.version,
        version_id=published.version_id,
        checksum=published.checksum,
        compiler_version=ir.compiler_version,
        schema_version=ir.schema_version,
        input_schema_digest=ir.input_schema_digest,
        result_schema_digest=ir.result.schema_digest if ir.result is not None else None,
        capability_pins=tuple(
            sorted(f"{b.capability_code}@{b.capability_version}" for b in ir.capability_bindings)
        ),
        permission_scopes=tuple(
            sorted({scope for b in ir.capability_bindings for scope in b.permission_scopes})
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebiz-crm-advise-publish",
        description="Publish crm@2, crm-advise@1 and crm-case-advise-on-demand@1 to the Registry",
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--actor-id", required=True, type=UUID)
    parser.add_argument(
        "--expect-digest",
        required=True,
        help="the reviewed IR4 checksum; the job fails unless the published checksum equals it",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--contract-root",
        default=None,
        help="override the installed crm_agent wheel resources (source checkouts only)",
    )
    return parser


async def _run_against_database(
    arguments: argparse.Namespace, database_url: str
) -> CrmAdvisePublicationReceipt:
    from agent_runtime.db.unit_of_work import UnitOfWork
    from agent_runtime.registry.capability_publication import CapabilityPublicationService
    from agent_runtime.registry.workflows import PostgresWorkflowRegistry
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    def unit_of_work_factory() -> UnitOfWork:
        return UnitOfWork(session_factory)

    try:
        return await publish_crm_advise(
            capabilities=CapabilityPublicationService(unit_of_work_factory),
            workflows=PostgresWorkflowRegistry(unit_of_work_factory),
            tenant_id=str(arguments.tenant_id),
            actor=ActorRef(actor_id=arguments.actor_id, actor_type="runtime-publisher"),
            root=Path(arguments.contract_root) if arguments.contract_root else None,
        )
    finally:
        await engine.dispose()


Runner = Callable[[argparse.Namespace, str], CrmAdvisePublicationReceipt]


def main(
    argv: Sequence[str] | None = None,
    *,
    database_url: str | None = None,
    run: Runner | None = None,
) -> int:
    """Publish and pin-check without exposing database or compiler exception details."""

    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    expected = arguments.expect_digest.strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        print(
            "crm-advise publish: FAIL --expect-digest must be a lowercase SHA-256", file=sys.stderr
        )
        return 2
    configured_url = database_url if database_url is not None else os.environ.get(_DATABASE_URL_ENV)
    if run is None and not configured_url:
        print("crm-advise publish: FAIL database is unavailable", file=sys.stderr)
        return 1
    try:
        if run is not None:
            receipt = run(arguments, configured_url or "")
        else:
            from agent_runtime.event_loop import configure_psycopg_event_loop_policy

            configure_psycopg_event_loop_policy()
            receipt = asyncio.run(_run_against_database(arguments, configured_url or ""))
    except CrmAdvisePublicationError as error:
        print(f"crm-advise publish: FAIL {error}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never expose raw database/provider details
        print("crm-advise publish: FAIL internal publication error", file=sys.stderr)
        return 1
    document = receipt.as_document()
    matches = receipt.checksum == expected
    document["expected_digest"] = expected
    document["status"] = "PASS" if matches else "DIGEST_MISMATCH"
    if arguments.format == "json":
        print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    else:
        for item in receipt.catalog_sets:
            print(
                f"catalog {item.set_id}@{item.version} digest={item.content_digest} "
                f"created={item.created} verified={item.verified}"
            )
        print(f"workflow {document['workflow']} version_id={receipt.version_id}")
        print(f"CRM_ADVISOR_WORKFLOW_DIGEST={receipt.checksum}")
        print(f"crm-advise publish: {document['status']}")
    if not matches:
        print(
            "crm-advise publish: FAIL published checksum differs from --expect-digest",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = [
    "CATALOG_MANIFESTS",
    "WORKFLOW_REF",
    "CapabilitySetPublisher",
    "CompiledAdviseWorkflow",
    "CrmAdvisePublicationError",
    "CrmAdvisePublicationReceipt",
    "agent_contract_root",
    "compile_advise_workflow",
    "load_catalog_publications",
    "main",
    "publish_crm_advise",
    "resource_bundle",
]
