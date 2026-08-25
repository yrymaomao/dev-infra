from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from uuid import uuid4

import agent_runtime
import jwt
import pytest
from agent_runtime.registry.capability_manifest import CapabilitySetPublication
from agent_runtime.registry.capability_publication import CapabilityPublicationReport

from ebiz_deployment.supply_chain_live import (
    LiveSmokeSettings,
    _verify_complete_branch_nodes,
    _verify_durable_smoke_facts,
    issue_smoke_token,
    load_supply_chain_capability_set,
    publish_capabilities,
    verify_complete_snapshot,
)
from ebiz_deployment.supply_chain_live import main as live_smoke_main

PROVIDER_VERSIONS = {
    "inventory-supply-chain.scope-validator": "1.0.0",
    "inventory-supply-chain.skill-loader": "1.0.0",
    "inventory-supply-chain.evidence-gate": "1.0.0",
    "inventory-supply-chain.forecast-engine": "1.0.0",
    "inventory-supply-chain.classification-engine": "1.0.0",
    "inventory-supply-chain.replenishment-engine": "1.0.0",
    "inventory-supply-chain.result-builder": "1.0.0",
    "inventory-supply-chain.blocked-result-builder": "1.0.0",
    "yeaher.erp": "0.1.0",
}

# Ordered by (code, version), matching the public capability-set projection.
EXPECTED_CAPABILITIES = (
    ("inventory.get_total_snapshot", "READ", "yeaher.erp", 30_000, 1, True),
    ("sales_profit.get_sku_windows", "READ", "yeaher.erp", 30_000, 1, True),
    (
        "supply_chain.build_agent_result",
        "PREVIEW",
        "inventory-supply-chain.result-builder",
        10_000,
        0,
        True,
    ),
    (
        "supply_chain.build_blocked_result",
        "PREVIEW",
        "inventory-supply-chain.blocked-result-builder",
        10_000,
        0,
        False,
    ),
    (
        "supply_chain.build_replenishment_proposal",
        "PREVIEW",
        "inventory-supply-chain.replenishment-engine",
        10_000,
        0,
        True,
    ),
    (
        "supply_chain.classify_inventory",
        "PREVIEW",
        "inventory-supply-chain.classification-engine",
        10_000,
        0,
        True,
    ),
    (
        "supply_chain.compute_forecast",
        "PREVIEW",
        "inventory-supply-chain.forecast-engine",
        10_000,
        0,
        True,
    ),
    (
        "supply_chain.evaluate_evidence",
        "READ",
        "inventory-supply-chain.evidence-gate",
        5_000,
        0,
        True,
    ),
    (
        "supply_chain.load_skill_input",
        "READ",
        "inventory-supply-chain.skill-loader",
        10_000,
        0,
        True,
    ),
    (
        "supply_chain.prepare_scope",
        "READ",
        "inventory-supply-chain.scope-validator",
        5_000,
        0,
        False,
    ),
)


def _assert_schema_is_self_contained(schema: object) -> None:
    if isinstance(schema, dict):
        assert "$ref" not in schema
        for value in schema.values():
            _assert_schema_is_self_contained(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_schema_is_self_contained(value)


def test_installed_supply_chain_contracts_project_ten_public_capabilities() -> None:
    publication = load_supply_chain_capability_set(provider_versions=PROVIDER_VERSIONS)

    assert isinstance(publication, CapabilitySetPublication)
    assert publication.spec_version == "ebizhub.capability-set/v1"
    assert publication.agent_id == "inventory-supply-chain"
    assert publication.version == 1
    assert len(publication.content_digest) == 64
    assert len(publication.capabilities) == 10
    assert tuple((item.code, item.version) for item in publication.capabilities) == tuple(
        (code, 1) for code, *_ in EXPECTED_CAPABILITIES
    )

    for item, expected in zip(publication.capabilities, EXPECTED_CAPABILITIES, strict=True):
        code, effect, provider_id, timeout_ms, retry_limit, evidence_required = expected
        assert item.code == code
        assert item.effect == effect
        assert item.provider_id == provider_id
        assert item.provider_version == PROVIDER_VERSIONS[provider_id]
        assert item.timeout_ms == timeout_ms
        assert item.retry_limit == retry_limit
        assert item.evidence_required is evidence_required
        assert item.idempotency_policy == "invocation"
        assert item.business_idempotency_required is False
        assert item.write_certification_status == "UNCERTIFIED"
        assert item.max_hosting_level == "ADVISORY"
        assert item.reversibility is None
        assert item.write_policy is None
        assert len(item.content_digest) == 64
        _assert_schema_is_self_contained(item.input_schema)
        _assert_schema_is_self_contained(item.output_schema)

    by_code = {item.code: item for item in publication.capabilities}
    inventory = by_code["inventory.get_total_snapshot"]
    assert inventory.permissions == ("inventory.read",)
    assert inventory.input_schema["additionalProperties"] is False
    assert set(inventory.input_schema["required"]) == {
        "run_id",
        "marketplace",
        "sku",
        "snapshot_time",
    }
    assert set(inventory.input_schema["properties"]) == {
        "run_id",
        "marketplace",
        "sku",
        "snapshot_time",
    }


def test_supply_chain_projection_is_deterministic() -> None:
    first = load_supply_chain_capability_set(provider_versions=PROVIDER_VERSIONS)
    second = load_supply_chain_capability_set(provider_versions=PROVIDER_VERSIONS)

    assert first.content_digest == second.content_digest
    assert tuple(item.content_digest for item in first.capabilities) == tuple(
        item.content_digest for item in second.capabilities
    )


def test_runtime_publisher_contains_no_supply_chain_business_code() -> None:
    runtime_root = Path(agent_runtime.__file__).resolve().parent
    offenders = [
        str(module)
        for module in sorted(runtime_root.rglob("*.py"))
        if "inventory_supply_chain" in module.read_text(encoding="utf-8")
    ]
    assert offenders == []

    distribution = metadata.distribution("ebizhub-agent-runtime")
    packaged = [str(item) for item in distribution.files or ()]
    assert packaged, "the Runtime distribution must expose its installed files"
    assert [item for item in packaged if "inventory_supply_chain" in item] == []


def test_smoke_token_carries_credential_reference_and_minimum_runtime_scopes() -> None:
    now = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
    secret = "s" * 32

    token = issue_smoke_token(
        tenant_id="tenant-live",
        credential_ref="session:credential:123",
        secret=secret,
        now=now,
    )

    claims = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience="agent-runtime",
        options={"verify_exp": False},
    )
    assert claims["tenant_id"] == "tenant-live"
    assert claims["credential_ref"] == "session:credential:123"
    assert claims["scopes"] == [
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
    ]
    assert claims["exp"] - claims["iat"] == 600


@pytest.mark.asyncio
async def test_capability_publication_is_durable_and_idempotent() -> None:
    database_url = os.environ.get("SUPPLY_CHAIN_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SUPPLY_CHAIN_TEST_DATABASE_URL is required")
    tenant_id = f"supply-chain-live-{uuid4()}"
    publication = load_supply_chain_capability_set(provider_versions=PROVIDER_VERSIONS)

    first = await publish_capabilities(
        database_url=database_url,
        tenant_id=tenant_id,
        provider_versions=PROVIDER_VERSIONS,
    )
    second = await publish_capabilities(
        database_url=database_url,
        tenant_id=tenant_id,
        provider_versions=PROVIDER_VERSIONS,
    )

    assert isinstance(first, CapabilityPublicationReport)
    assert isinstance(second, CapabilityPublicationReport)
    assert first.created == 10
    assert first.verified == 0
    assert second.created == 0
    assert second.verified == 10
    assert first.capability_set_digest == publication.content_digest
    assert second.capability_set_digest == publication.content_digest
    expected_identities = tuple(f"{code}@1" for code, *_ in EXPECTED_CAPABILITIES)
    assert first.published == expected_identities
    assert second.published == expected_identities


def test_live_smoke_settings_require_explicit_real_system_inputs() -> None:
    environ = {
        "SUPPLY_CHAIN_SMOKE_API_URL": "http://127.0.0.1:8000",
        "SUPPLY_CHAIN_TENANT_ID": "tenant-live",
        "SUPPLY_CHAIN_CREDENTIAL_REF": "login:session:123",
        "SUPPLY_CHAIN_MARKETPLACE": "US",
        "SUPPLY_CHAIN_SKU": "SKU-LIVE",
        "SUPPLY_CHAIN_SKILL_INPUT_REF": "SKU-LIVE.yaml",
        "SUPPLY_CHAIN_SNAPSHOT_TIME": "2026-08-24T08:00:00Z",
        "SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT": "10",
        "APP_DATABASE_URL": (
            "postgresql+asyncpg://agent_runtime_test:agent_runtime_test@127.0.0.1:5433/"
            "agent_runtime_test"
        ),
        "APP_JWT_SECRET": "j" * 32,
        "ERP_PACKAGE_VERSION": "0.1.0",
        "LOCAL_DEV_E2E": "true",
    }

    settings = LiveSmokeSettings.from_environment(environ)

    assert settings.tenant_id == "tenant-live"
    assert settings.inputs == {
        "run_id": settings.run_id,
        "marketplace": "US",
        "sku": "SKU-LIVE",
        "snapshot_time": "2026-08-24T08:00:00Z",
        "skill_input_ref": "SKU-LIVE.yaml",
    }
    assert settings.provider_versions["yeaher.erp"] == "0.1.0"
    assert settings.provider_versions["inventory-supply-chain.scope-validator"] == "1.0.0"
    assert settings.expected_evidence_count == 10
    assert settings.local_dev_e2e is True
    assert settings.call_provenance == {
        "local_dev_e2e": True,
        "real_erp_calls": False,
        "production_model_calls": False,
    }
    assert "j" * 32 not in repr(settings)


def test_live_smoke_settings_reject_missing_credential_reference() -> None:
    with pytest.raises(ValueError, match="SUPPLY_CHAIN_CREDENTIAL_REF"):
        LiveSmokeSettings.from_environment({})


def test_live_smoke_accepts_only_the_complete_result_branch() -> None:
    result = verify_complete_snapshot(
        {
            "status": "SUCCEEDED",
            "outputs": {"complete_result": {"status": "COMPLETE", "run_id": "run-1"}},
        }
    )

    assert result == "complete_result"


def test_local_smoke_requires_only_the_complete_terminal_node() -> None:
    assert _verify_complete_branch_nodes({"build_result": "SUCCEEDED"}) == "complete_result"

    with pytest.raises(ValueError, match="complete_result"):
        _verify_complete_branch_nodes({"build_blocked_result": "SUCCEEDED"})
    with pytest.raises(ValueError, match="complete_result"):
        _verify_complete_branch_nodes(
            {"build_result": "SUCCEEDED", "build_blocked_result": "SUCCEEDED"}
        )


def test_local_timeout_recovery_requires_complete_runtime_facts() -> None:
    events = [
        "session.started",
        "workflow.started",
        "capability.call.started",
        "evidence.attached",
        "capability.call.completed",
        "workflow.completed",
    ]
    _verify_durable_smoke_facts(
        status="SUCCEEDED",
        event_types=events,
        evidence_count=13,
        expected_evidence_count=13,
    )

    with pytest.raises(ValueError, match="evidence count"):
        _verify_durable_smoke_facts(
            status="SUCCEEDED",
            event_types=events,
            evidence_count=12,
            expected_evidence_count=13,
        )


@pytest.mark.parametrize(
    "snapshot",
    [
        {"status": "FAILED", "outputs": None},
        {"status": "SUCCEEDED", "outputs": {"blocked_result": {"status": "BLOCKED"}}},
        {
            "status": "SUCCEEDED",
            "outputs": {
                "complete_result": {"status": "COMPLETE"},
                "blocked_result": {"status": "BLOCKED"},
            },
        },
    ],
)
def test_live_smoke_rejects_failed_blocked_or_ambiguous_results(
    snapshot: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="complete_result"):
        verify_complete_snapshot(snapshot)


def test_console_script_rejects_process_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ebiz-supply-chain-live-smoke", "--help"])

    assert live_smoke_main() == 2
