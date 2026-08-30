from __future__ import annotations

import sys
from datetime import UTC, datetime

import jwt
import pytest

from ebiz_deployment.supply_chain_live import (
    LiveSmokeSettings,
    issue_smoke_token,
    main,
    verify_terminal_result,
)


def local_environment() -> dict[str, str]:
    return {
        "SUPPLY_CHAIN_SMOKE_API_URL": "http://127.0.0.1:8000",
        "SUPPLY_CHAIN_TENANT_ID": "tenant-local",
        "SUPPLY_CHAIN_CREDENTIAL_REF": "local:credential:123",
        "SUPPLY_CHAIN_MARKET_SCOPE": "NA_COMPANY",
        "SUPPLY_CHAIN_SKU": "SKU-LOCAL",
        "SUPPLY_CHAIN_SNAPSHOT_TIME": "2026-08-30T12:00:00Z",
        "SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT": "4",
        "SUPPLY_CHAIN_RUN_ID": "supply-chain-v4-local-1",
        "APP_JWT_SECRET": "j" * 32,
        "LOCAL_DEV_E2E": "true",
    }


def test_settings_pin_v4_na_company_fbm_auto_and_non_production_provenance() -> None:
    settings = LiveSmokeSettings.from_environment(local_environment())

    assert settings.agent_id == "inventory-supply-chain"
    assert settings.agent_version == 4
    assert settings.workflow_code == "inventory-supply-chain-daily"
    assert settings.workflow_version == 4
    assert settings.inputs == {
        "run_id": "supply-chain-v4-local-1",
        "scope": {
            "market_scope": "NA_COMPANY",
            "sku": "SKU-LOCAL",
            "snapshot_time": "2026-08-30T12:00:00Z",
            "fulfillment_mode": "AUTO",
        },
    }
    assert settings.call_provenance == {
        "local_dev_e2e": True,
        "real_erp_calls": False,
        "production_model_calls": False,
        "production_e2e_verified": False,
    }
    assert "j" * 32 not in repr(settings)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("SUPPLY_CHAIN_MARKET_SCOPE", "US", "NA_COMPANY"),
        ("LOCAL_DEV_E2E", "false", "real-dev/UAT"),
    ],
)
def test_settings_fail_closed_outside_v4_local_scope(key: str, value: str, message: str) -> None:
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
    assert all("write" not in scope.lower() for scope in claims["scopes"])


@pytest.mark.parametrize("status", ["COMPLETE", "BLOCKED"])
def test_terminal_result_accepts_schema4_result(status: str) -> None:
    snapshot = {"status": "SUCCEEDED", "result": {"status": status}}
    assert verify_terminal_result(snapshot) == status


@pytest.mark.parametrize(
    "snapshot",
    [
        {"status": "FAILED", "result": None},
        {"status": "SUCCEEDED", "outputs": {"complete_result": {}}},
        {"status": "SUCCEEDED", "result": {"status": "PARTIAL"}},
        {
            "status": "SUCCEEDED",
            "result": {"status": "COMPLETE"},
            "complete_result": {},
        },
    ],
)
def test_terminal_result_rejects_failure_partial_and_legacy(snapshot: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="v4 smoke"):
        verify_terminal_result(snapshot)


def test_console_script_only_validates_local_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["ebiz-supply-chain-live-smoke"])
    for key, value in local_environment().items():
        monkeypatch.setenv(key, value)

    assert main() == 0
    output = capsys.readouterr().out
    assert '"agent": "inventory-supply-chain@4"' in output
    assert '"production_e2e_verified": false' in output
