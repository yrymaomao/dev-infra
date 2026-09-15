"""The advise publish job: two Catalog sets, one compiled workflow, one pinned checksum."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from ebiz_runtime_contracts import ActorRef, canonical_json_bytes
from workflow_runtime.canonical.serializer import serialize_ir
from workflow_runtime.registry.contracts import (
    DraftWorkflowRequest,
    PublishedWorkflow,
    WorkflowVersion,
    WorkflowVersionNotFound,
)

from ebiz_deployment import crm_advise_publication as job

#: The reviewed IR4 checksum of crm-case-advise-on-demand@1 (crm-agent branch
#: codex/crm-agent-openclaw-advise, tools/compile_advise_workflow.py).
EXPECTED_DIGEST = "54f9a85048b024ca6b488abce7fc2bd3adb106a363a1c58b8ef4422e701a4095"
EXPECTED_RESULT_SCHEMA_DIGEST = "d8446e275e70703870d1964ae36fca838e9a2d37bac11d3ff5bef1e782861372"
EXPECTED_INPUT_SCHEMA_DIGEST = "3cd12b51a1a9b94a0476883c59ff4d9fd8111e73420da1eea2dc8ea959c2222f"
ACTOR = ActorRef(actor_id=UUID("00000000-0000-4000-8000-000000000042"), actor_type="service")

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("crm_agent") is None,
    reason="ebiz-agent-crm is not installed in this environment",
)


class FakeCapabilityPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.seen: set[tuple[str, int]] = set()

    async def publish_set(self, *, tenant_id, publication, actor, trace_id):
        identity = (publication.set_id, publication.version)
        replay = identity in self.seen
        self.seen.add(identity)
        self.calls.append((tenant_id, publication.set_id, publication.version))
        count = len(publication.capabilities)
        return SimpleNamespace(
            created=0 if replay else count,
            verified=count if replay else 0,
            capability_set_digest=publication.content_digest,
        )


class InMemoryWorkflowRegistry:
    """The WorkflowPublicationRegistry port, immutable once published."""

    def __init__(self) -> None:
        self.versions: dict[tuple[str, str, int], WorkflowVersion] = {}
        self.irs: dict[UUID, dict] = {}
        self.published: dict[tuple[str, str, int], PublishedWorkflow] = {}
        self.definition_ids: dict[tuple[str, str], UUID] = {}

    async def create_draft(self, request: DraftWorkflowRequest) -> WorkflowVersion:
        key = (request.tenant_id, request.code, request.version)
        if key in self.versions:
            raise AssertionError("draft created twice")
        source = request.source_yaml.encode("utf-8")
        version = WorkflowVersion(
            version_id=uuid4(),
            tenant_id=request.tenant_id,
            workflow_definition_id=self.definition_ids.setdefault(key[:2], uuid4()),
            code=request.code,
            version=request.version,
            status="DRAFT",
            content_digest=hashlib.sha256(source).hexdigest(),
            compiler_version="",
            row_version=0,
            definition_row_version=0,
            resources=request.resources,
            source_sha256=hashlib.sha256(source).hexdigest(),
        )
        self.versions[key] = version
        return version

    async def get_version(self, tenant_id: str, code: str, version: int) -> WorkflowVersion:
        try:
            return self.versions[(tenant_id, code, version)]
        except KeyError:
            raise WorkflowVersionNotFound() from None

    async def save_validated(self, tenant_id: str, version_id: UUID, ir) -> WorkflowVersion:
        key = next(k for k, v in self.versions.items() if v.version_id == version_id)
        validated = self.versions[key].model_copy(
            update={
                "status": "VALIDATED",
                "content_digest": ir.checksum,
                "compiler_version": ir.compiler_version,
                "row_version": 1,
            }
        )
        self.versions[key] = validated
        self.irs[version_id] = json.loads(serialize_ir(ir))
        return validated

    async def publish_inactive(
        self, tenant_id: str, version_id: UUID, actor: ActorRef, *, trace_id: str = "untraced"
    ) -> PublishedWorkflow:
        key = next(k for k, v in self.versions.items() if v.version_id == version_id)
        version = self.versions[key]
        self.versions[key] = version.model_copy(update={"status": "PUBLISHED", "row_version": 2})
        published = PublishedWorkflow(
            version_id=version_id,
            tenant_id=tenant_id,
            workflow_definition_id=version.workflow_definition_id,
            code=version.code,
            version=version.version,
            canonical_ir=self.irs[version_id],
            checksum=version.content_digest,
            compiler_version=version.compiler_version,
            published_at=datetime.now(UTC),
            definition_row_version=1,
            resources=version.resources,
        )
        self.published[key] = published
        return published

    async def get_published(self, tenant_id: str, code: str, version: int) -> PublishedWorkflow:
        return self.published[(tenant_id, code, version)]


def source_root() -> Path:
    return job.agent_contract_root()


def test_the_wheel_carries_both_catalog_sets_and_the_advise_workflow() -> None:
    publications = job.load_catalog_publications(source_root())
    assert [(p.set_id, p.version) for p in publications] == [("crm", 2), ("crm-advise", 1)]
    advise = publications[1]
    assert sorted(item.code for item in advise.capabilities) == [
        "crm.build_blocked_advice",
        "crm.build_case_advice",
        "crm.build_takeover_advice",
        "crm.prepare_on_demand_trigger",
    ]
    assert all(item.effect in {"READ", "PREVIEW"} for item in advise.capabilities)


def test_compiled_advise_workflow_is_the_reviewed_read_only_ir4() -> None:
    root = source_root()
    compiled = job.compile_advise_workflow(root, job.load_catalog_publications(root))
    ir = compiled.ir
    assert (ir.workflow_identity.code, ir.workflow_identity.version) == (
        "crm-case-advise-on-demand",
        1,
    )
    assert ir.schema_version == 4 and ir.compiler_version == "1.3.0"
    assert ir.workflow_identity.risk_level == "preview"
    assert not ir.governance_policy.write_capability_nodes
    assert {b.effect for b in ir.capability_bindings} <= {"READ", "PREVIEW"}
    assert ir.input_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"case_id": {"type": "string"}},
        "required": ["case_id"],
    }
    assert ir.input_schema_digest == EXPECTED_INPUT_SCHEMA_DIGEST
    assert ir.result is not None and ir.result.schema_digest == EXPECTED_RESULT_SCHEMA_DIGEST
    assert ir.checksum == EXPECTED_DIGEST
    scopes = {scope for b in ir.capability_bindings for scope in b.permission_scopes}
    assert scopes == {
        "crm.case.read",
        "crm.context.read",
        "crm.evidence.read",
        "crm.capability.read",
        "crm.compute",
        "crm.preview",
    }


@pytest.mark.asyncio
async def test_publish_job_is_idempotent_and_pins_the_expected_checksum() -> None:
    capabilities = FakeCapabilityPublisher()
    workflows = InMemoryWorkflowRegistry()
    receipt = await job.publish_crm_advise(
        capabilities=capabilities,
        workflows=workflows,
        tenant_id="tenant-a",
        actor=ACTOR,
        root=source_root(),
    )
    assert [(c.set_id, c.version, c.created > 0, c.verified) for c in receipt.catalog_sets] == [
        ("crm", 2, True, 0),
        ("crm-advise", 1, True, 0),
    ]
    assert capabilities.calls == [("tenant-a", "crm", 2), ("tenant-a", "crm-advise", 1)]
    assert receipt.checksum == EXPECTED_DIGEST
    assert receipt.result_schema_digest == EXPECTED_RESULT_SCHEMA_DIGEST
    assert receipt.input_schema_digest == EXPECTED_INPUT_SCHEMA_DIGEST
    assert receipt.workflow_code == "crm-case-advise-on-demand" and receipt.workflow_version == 1
    published = workflows.published[("tenant-a", "crm-case-advise-on-demand", 1)]
    assert published.checksum == EXPECTED_DIGEST
    assert canonical_json_bytes(published.canonical_ir)
    # A second run verifies the sets and re-reads the published workflow: no new rows.
    again = await job.publish_crm_advise(
        capabilities=capabilities,
        workflows=workflows,
        tenant_id="tenant-a",
        actor=ACTOR,
        root=source_root(),
    )
    assert [(c.created, c.verified > 0) for c in again.catalog_sets] == [(0, True), (0, True)]
    assert again.version_id == receipt.version_id and again.checksum == EXPECTED_DIGEST
    assert len(workflows.versions) == 1


def test_cli_prints_the_digest_and_fails_on_a_mismatch(capsys) -> None:
    def run(arguments, database_url):
        assert arguments.tenant_id == "tenant-a"
        workflows = InMemoryWorkflowRegistry()

        async def go():
            return await job.publish_crm_advise(
                capabilities=FakeCapabilityPublisher(),
                workflows=workflows,
                tenant_id=arguments.tenant_id,
                actor=ActorRef(actor_id=arguments.actor_id, actor_type="runtime-publisher"),
                root=source_root(),
            )

        import asyncio

        return asyncio.run(go())

    argv = ["--tenant-id", "tenant-a", "--actor-id", str(ACTOR.actor_id), "--format", "json"]
    assert job.main([*argv, "--expect-digest", EXPECTED_DIGEST], run=run) == 0
    document = json.loads(capsys.readouterr().out.strip())
    assert document["status"] == "PASS"
    assert document["CRM_ADVISOR_WORKFLOW_DIGEST"] == EXPECTED_DIGEST
    assert document["catalog_sets"][1]["set_id"] == "crm-advise"

    assert job.main([*argv, "--expect-digest", "f" * 64], run=run) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip())["status"] == "DIGEST_MISMATCH"
    assert "differs from --expect-digest" in captured.err

    assert job.main([*argv, "--expect-digest", "not-a-digest"], run=run) == 2


def test_cli_refuses_to_run_without_a_database() -> None:
    assert (
        job.main(
            ["--tenant-id", "t", "--actor-id", str(ACTOR.actor_id), "--expect-digest", "a" * 64],
            database_url="",
        )
        == 1
    )
