from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from agent_runtime.registry.capability_manifest import load_capability_publication
from ebiz_runtime_contracts import EvidenceRef
from workflow_runtime.compiler.contracts import CapabilityDescriptor, CapabilityNotPublishedError

from ebiz_deployment.local_skill_seed import (
    FIXTURE_CAPABILITY_CODE,
    LocalSkillSeedError,
    main,
    seed_governed_skill,
)


class _Registry:
    def __init__(self, descriptor: CapabilityDescriptor | None) -> None:
        self.descriptor = descriptor

    async def resolve_published(
        self, tenant_id: str, code: str, version: int
    ) -> CapabilityDescriptor:
        assert tenant_id
        if self.descriptor is None:
            raise CapabilityNotPublishedError(code, version)
        return self.descriptor


class _PayloadStore:
    def __init__(self) -> None:
        self.payload: Any = None
        self.committed = False

    async def put_exact_restricted(self, **kwargs: object) -> object:
        from agent_runtime.payloads.contracts import (
            PayloadClassification,
            PayloadWriteResult,
        )

        self.payload = kwargs["payload"]
        data = b'{"policy_version":"local-dev-v4"}'
        return PayloadWriteResult(
            inline_payload=None,
            payload_ref="memory://payloads/v1/" + "a" * 64,
            payload_hash=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            content_type="application/json",
            classification=PayloadClassification.RESTRICTED,
            required_permission=str(kwargs["required_permission"]),
        )

    async def ensure_committed(self, **kwargs: object) -> None:
        self.committed = True


class _EvidenceStore:
    def __init__(self, *, returned_tenant: str | None = None) -> None:
        self.returned_tenant = returned_tenant
        self.items: list[EvidenceRef] = []

    async def put(self, evidence: EvidenceRef) -> EvidenceRef:
        self.items.append(evidence)
        if self.returned_tenant is None:
            return evidence
        return evidence.model_copy(update={"tenant_id": self.returned_tenant})


def _descriptor() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        code=FIXTURE_CAPABILITY_CODE,
        version=1,
        content_digest="b" * 64,
        effect="READ",
        input_schema={"type": "object", "additionalProperties": False},
        input_schema_ref="capability://fixture/input/v1",
        output_schema={"type": "object", "additionalProperties": False},
        output_schema_ref="capability://fixture/output/v1",
        permission_scopes=["deployment.fixture.read"],
        evidence_required=True,
        idempotency_required=False,
        approval_required=False,
        retryable_error_codes=[],
        timeout_seconds_limit=1,
        runtime_binding="deployment.fixture.governed-artifact@1.0.0",
    )


@pytest.mark.asyncio
async def test_seed_uses_published_fixture_and_is_stable(tmp_path: Path) -> None:
    skill = tmp_path / "skill.json"
    skill.write_text('{"policy_version":"local-dev-v4"}', encoding="utf-8")
    digest = hashlib.sha256(skill.read_bytes()).hexdigest()
    payload_store = _PayloadStore()
    evidence_store = _EvidenceStore()

    first = await seed_governed_skill(
        app_env="local_dev",
        local_dev_e2e=True,
        tenant_id="tenant-a",
        skill_path=skill,
        expected_sha256=digest,
        captured_at=datetime(2026, 8, 24, tzinfo=UTC),
        registry=_Registry(_descriptor()),
        payload_store=payload_store,  # type: ignore[arg-type]
        evidence_store=evidence_store,
    )
    second = await seed_governed_skill(
        app_env="local_dev",
        local_dev_e2e=True,
        tenant_id="tenant-a",
        skill_path=skill,
        expected_sha256=digest,
        captured_at=datetime(2026, 8, 24, tzinfo=UTC),
        registry=_Registry(_descriptor()),
        payload_store=payload_store,  # type: ignore[arg-type]
        evidence_store=evidence_store,
    )

    assert first == second
    assert payload_store.committed is True
    assert evidence_store.items[0].capability_ref.resource_id == FIXTURE_CAPABILITY_CODE
    assert evidence_store.items[0].tenant_id == "tenant-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_env", "local_dev_e2e"), [("production", True), ("real_dev", True), ("local_dev", False)]
)
async def test_seed_refuses_non_deterministic_local_modes(
    tmp_path: Path, app_env: str, local_dev_e2e: bool
) -> None:
    skill = tmp_path / "skill.json"
    skill.write_text("{}", encoding="utf-8")
    with pytest.raises(LocalSkillSeedError, match="LOCAL_DEV_E2E"):
        await seed_governed_skill(
            app_env=app_env,
            local_dev_e2e=local_dev_e2e,
            tenant_id="tenant-a",
            skill_path=skill,
            expected_sha256=hashlib.sha256(skill.read_bytes()).hexdigest(),
            captured_at=datetime(2026, 8, 24, tzinfo=UTC),
            registry=_Registry(_descriptor()),
            payload_store=_PayloadStore(),  # type: ignore[arg-type]
            evidence_store=_EvidenceStore(),
        )


@pytest.mark.asyncio
async def test_seed_rejects_tampered_skill_hash(tmp_path: Path) -> None:
    skill = tmp_path / "skill.json"
    skill.write_text("{}", encoding="utf-8")
    with pytest.raises(LocalSkillSeedError, match="digest"):
        await seed_governed_skill(
            app_env="local_dev",
            local_dev_e2e=True,
            tenant_id="tenant-a",
            skill_path=skill,
            expected_sha256="0" * 64,
            captured_at=datetime(2026, 8, 24, tzinfo=UTC),
            registry=_Registry(_descriptor()),
            payload_store=_PayloadStore(),  # type: ignore[arg-type]
            evidence_store=_EvidenceStore(),
        )


@pytest.mark.asyncio
async def test_seed_rejects_unpublished_fixture_capability(tmp_path: Path) -> None:
    skill = tmp_path / "skill.json"
    skill.write_text("{}", encoding="utf-8")
    with pytest.raises(LocalSkillSeedError, match="published"):
        await seed_governed_skill(
            app_env="local_dev",
            local_dev_e2e=True,
            tenant_id="tenant-a",
            skill_path=skill,
            expected_sha256=hashlib.sha256(skill.read_bytes()).hexdigest(),
            captured_at=datetime(2026, 8, 24, tzinfo=UTC),
            registry=_Registry(None),
            payload_store=_PayloadStore(),  # type: ignore[arg-type]
            evidence_store=_EvidenceStore(),
        )


@pytest.mark.asyncio
async def test_seed_fails_closed_on_cross_tenant_store_result(tmp_path: Path) -> None:
    skill = tmp_path / "skill.json"
    skill.write_text("{}", encoding="utf-8")
    with pytest.raises(LocalSkillSeedError, match="tenant"):
        await seed_governed_skill(
            app_env="local_dev",
            local_dev_e2e=True,
            tenant_id="tenant-a",
            skill_path=skill,
            expected_sha256=hashlib.sha256(skill.read_bytes()).hexdigest(),
            captured_at=datetime(2026, 8, 24, tzinfo=UTC),
            registry=_Registry(_descriptor()),
            payload_store=_PayloadStore(),  # type: ignore[arg-type]
            evidence_store=_EvidenceStore(returned_tenant="tenant-b"),
        )


def test_fixture_package_is_separate_and_not_a_production_pin() -> None:
    root = Path(__file__).parents[1]
    project = (root / "packages/local-evidence-fixture/pyproject.toml").read_text(
        encoding="utf-8"
    )
    catalog = (
        root / "packages/local-evidence-fixture/contracts/capabilities.yaml"
    ).read_text(encoding="utf-8")
    root_project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"deployment.fixture.governed-artifact"' in project
    assert "deployment.fixture.seed_governed_artifact" in catalog
    assert "ebiz-deployment-local-evidence-fixture" not in root_project


def test_fixture_catalog_loads_through_runtime_publisher_contract() -> None:
    root = Path("packages/local-evidence-fixture/contracts").resolve()
    publication = load_capability_publication(
        manifest_path=root / "capabilities.yaml",
        contract_root=root,
        provider_versions={"deployment.fixture.governed-artifact": "1.0.0"},
    )
    assert publication.set_id == "deployment.fixture.local-evidence"
    assert [(item.code, item.version) for item in publication.capabilities] == [
        (FIXTURE_CAPABILITY_CODE, 1)
    ]


def test_seed_cli_prints_only_uuid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = UUID("00000000-0000-5000-8000-000000000001")

    async def fake_run(arguments: object) -> UUID:
        del arguments
        return expected

    monkeypatch.setattr("ebiz_deployment.local_skill_seed._run", fake_run)
    result = main(
        [
            "--tenant-id",
            "tenant-a",
            "--skill-file",
            "skill.json",
            "--expected-sha256",
            "0" * 64,
            "--captured-at",
            "2026-08-24T00:00:00Z",
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == f"{expected}\n"
    assert captured.err == ""
