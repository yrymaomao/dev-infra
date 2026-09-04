from __future__ import annotations

import sys
from datetime import UTC, datetime

import httpx
import jwt
import pytest

from ebiz_deployment.supply_chain_live import (
    LiveSmokeSettings,
    issue_smoke_token,
    main,
    run_runtime_smoke,
    verify_terminal_result,
)


def local_environment() -> dict[str, str]:
    return {
        "SUPPLY_CHAIN_SMOKE_API_URL": "http://127.0.0.1:8000",
        "SUPPLY_CHAIN_TENANT_ID": "tenant-local",
        "SUPPLY_CHAIN_CREDENTIAL_REF": "local:credential:123",
        "SUPPLY_CHAIN_MARKET_SCOPE": "NA_COMPANY",
        "SUPPLY_CHAIN_SKU": "SKU-LOCAL",
        "SUPPLY_CHAIN_SKILL_INPUT_REF": "00000000-0000-4000-8000-000000000010",
        "SUPPLY_CHAIN_SNAPSHOT_TIME": "2026-08-30T12:00:00Z",
        "SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT": "5",
        "SUPPLY_CHAIN_EXPECTED_RESULT_STATUS": "COMPLETE",
        "SUPPLY_CHAIN_RUN_ID": "supply-chain-v5-local-1",
        "APP_JWT_SECRET": "j" * 32,
        "LOCAL_DEV_E2E": "true",
    }


def evidence(index: int) -> dict[str, object]:
    resource = {
        "resource_type": "capability",
        "resource_id": f"cap-{index}",
        "version": 1,
        "digest": f"{index + 1:x}" * 64,
    }
    return {
        "evidence_id": f"00000000-0000-4000-8000-{index + 1:012d}",
        "tenant_id": "tenant-local",
        "source_type": "ERP",
        "source_system": "local",
        "external_object_id": f"object-{index}",
        "captured_at": "2026-08-30T12:00:00Z",
        "freshness_at": "2026-08-30T12:00:00Z",
        "content_ref": f"payload-{index}",
        "content_hash": f"{index + 1:x}" * 64,
        "content_type": "application/json",
        "size_bytes": 10,
        "access_level": "supply_chain.read",
        "retention_policy": "local-test",
        "schema_version": 1,
        "capability_ref": resource,
        "plugin_ref": None,
        "execution_id": None,
        "trace_id": "trace-local",
    }


def blocked_result(count: int) -> dict[str, object]:
    return {
        "tenant_id": "tenant-local",
        "status": "BLOCKED",
        "scope": {"market_scope": "NA_COMPANY", "sku": "SKU-LOCAL"},
        "payload": None,
        "evidence": [evidence(index) for index in range(count)],
        "issues": [
            {
                "code": "LOCAL_TEST_BLOCK",
                "message": "Deterministic test block.",
                "blocking": True,
                "metadata": {},
            }
        ],
    }


def complete_result(count: int) -> dict[str, object]:
    return {
        "tenant_id": "tenant-local",
        "status": "COMPLETE",
        "scope": {"market_scope": "NA_COMPANY", "sku": "SKU-LOCAL"},
        "payload": {
            "forecast": {
                "mean_daily_demand": 2,
                "demand_variance": 0.25,
                "baseline_mean": 2,
                "baseline_variance": 0.25,
                "raw_cv": 0.25,
                "risk_cv": 0.25,
                "horizon_days": 49,
                "risk_flags": [],
            },
            "classification": {
                "status": "AVAILABLE",
                "quadrant": "CASH_COW",
                "adjusted_demand_scale": 1,
                "demand_score": 0.8,
                "growth_score": 0.5,
                "reason": "risk-adjusted cohort percentile",
            },
            "route": {
                "action": "HOLD",
                "inventory_position": 30,
                "protection_target": 20,
                "projected_coverage_days": 15,
                "below_protection": False,
                "profit_ok": True,
                "coverage_excess": False,
                "clearance_allowed": True,
                "risk_flags": [],
            },
            "risk_flags": [],
        },
        "evidence": [evidence(index) for index in range(count)],
        "issues": [],
    }


def test_settings_match_flat_v5_agent_input() -> None:
    settings = LiveSmokeSettings.from_environment(local_environment())
    assert settings.inputs == {
        "run_id": "supply-chain-v5-local-1",
        "market_scope": "NA_COMPANY",
        "sku": "SKU-LOCAL",
        "snapshot_time": "2026-08-30T12:00:00Z",
        "skill_input_ref": "00000000-0000-4000-8000-000000000010",
        "fulfillment_mode": "FBM",
    }
    assert settings.call_provenance["production_e2e_verified"] is False
    assert settings.expected_result_status == "COMPLETE"
    assert "j" * 32 not in repr(settings)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("SUPPLY_CHAIN_MARKET_SCOPE", "US", "NA_COMPANY"),
        ("LOCAL_DEV_E2E", "false", "real-dev/UAT"),
        ("SUPPLY_CHAIN_EXPECTED_RESULT_STATUS", "UNKNOWN", "COMPLETE or BLOCKED"),
    ],
)
def test_settings_fail_closed_outside_v5_local_scope(key: str, value: str, message: str) -> None:
    environ = local_environment()
    environ[key] = value
    with pytest.raises(ValueError, match=message):
        LiveSmokeSettings.from_environment(environ)


def test_token_has_only_read_preview_and_workflow_scopes() -> None:
    token = issue_smoke_token(
        tenant_id="tenant-local",
        credential_ref="local:credential:123",
        secret="s" * 32,
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )
    claims = jwt.decode(
        token,
        "s" * 32,
        algorithms=["HS256"],
        audience="agent-runtime",
        options={"verify_exp": False},
    )
    assert claims["scopes"] == [
        "inventory.read",
        "sales_profit.read",
        "supply_chain.preview",
        "workflow:read",
        "workflow:start",
    ]


def test_terminal_result_validates_public_schema_and_evidence_count() -> None:
    snapshot = {"status": "SUCCEEDED", "outputs": {"result": blocked_result(5)}}
    assert (
        verify_terminal_result(
            snapshot, expected_evidence_count=5, expected_result_status="BLOCKED"
        )
        == "BLOCKED"
    )
    with pytest.raises(ValueError, match="expected result status"):
        verify_terminal_result(
            snapshot, expected_evidence_count=5, expected_result_status="COMPLETE"
        )
    with pytest.raises(ValueError, match="evidence count"):
        verify_terminal_result(
            snapshot, expected_evidence_count=3, expected_result_status="BLOCKED"
        )
    complete = {"status": "SUCCEEDED", "outputs": {"result": complete_result(5)}}
    assert (
        verify_terminal_result(
            complete, expected_evidence_count=5, expected_result_status="COMPLETE"
        )
        == "COMPLETE"
    )


@pytest.mark.asyncio
async def test_runtime_smoke_calls_published_agent_execution_api() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agent-executions"
        body = __import__("json").loads(request.content)
        seen.append(body)
        return httpx.Response(
            201,
            json={
                "execution_id": "00000000-0000-4000-8000-000000000020",
                "status": "SUCCEEDED",
                "outputs": {"result": complete_result(5)},
            },
        )

    result = await run_runtime_smoke(
        LiveSmokeSettings.from_environment(local_environment()),
        transport=httpx.MockTransport(handler),
    )
    assert result["result_status"] == "COMPLETE"
    assert seen[0]["agent"] == {"id": "inventory-supply-chain", "version": 5}
    assert seen[0]["inputs"]["skill_input_ref"].endswith("0010")


@pytest.mark.asyncio
async def test_runtime_smoke_retries_only_pending_payload_finalization_with_same_request() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agent-executions"
        body = __import__("json").loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": "0"},
                json={
                    "error_code": "RUNTIME_PAYLOAD_FINALIZATION_PENDING",
                    "retryable": True,
                },
            )
        return httpx.Response(
            201,
            json={
                "execution_id": "00000000-0000-4000-8000-000000000020",
                "status": "SUCCEEDED",
                "outputs": {"result": complete_result(5)},
            },
        )

    result = await run_runtime_smoke(
        LiveSmokeSettings.from_environment(local_environment()),
        transport=httpx.MockTransport(handler),
    )

    assert result["result_status"] == "COMPLETE"
    assert len(seen) == 2
    assert seen[0] == seen[1]


@pytest.mark.asyncio
async def test_runtime_smoke_does_not_retry_other_or_nonretryable_503() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={
                "error_code": "RUNTIME_PAYLOAD_FINALIZATION_PENDING",
                "retryable": False,
            },
        )

    with pytest.raises(httpx.HTTPStatusError):
        await run_runtime_smoke(
            LiveSmokeSettings.from_environment(local_environment()),
            transport=httpx.MockTransport(handler),
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_runtime_smoke_bounds_pending_payload_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            headers={"Retry-After": "0"},
            json={
                "error_code": "RUNTIME_PAYLOAD_FINALIZATION_PENDING",
                "retryable": True,
            },
        )

    monkeypatch.setattr("ebiz_deployment.supply_chain_live._START_MAX_ATTEMPTS", 2)
    with pytest.raises(httpx.HTTPStatusError):
        await run_runtime_smoke(
            LiveSmokeSettings.from_environment(local_environment()),
            transport=httpx.MockTransport(handler),
        )
    assert calls == 2


def test_console_script_executes_runtime_smoke(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run(settings: LiveSmokeSettings) -> dict[str, object]:
        return {"execution_id": settings.run_id, "result_status": "COMPLETE"}

    monkeypatch.setattr(sys, "argv", ["ebiz-supply-chain-live-smoke"])
    monkeypatch.setattr("ebiz_deployment.supply_chain_live.run_runtime_smoke", fake_run)
    for key, value in local_environment().items():
        monkeypatch.setenv(key, value)
    assert main() == 0
    assert '"result_status": "COMPLETE"' in capsys.readouterr().out
