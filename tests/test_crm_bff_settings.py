"""BFF_CRM_* environment loading: the CRM profile is off by default and fails closed."""

from __future__ import annotations

import pytest

from ebiz_deployment.supply_chain_bff.config import BffSettings

BASE_ENV = {
    "BFF_POSTGRESQL_URL": "postgresql+asyncpg://user:pass@127.0.0.1:5432/bff",
    "BFF_CURSOR_HMAC_SIGNING_KEY": "k" * 32,
    "APP_JWT_SECRET": "j" * 32,
    "BFF_RUNTIME_URL": "http://127.0.0.1:8000",
    "SUPPLY_CHAIN_CREDENTIAL_REF": "opaque:runtime-service",
    "BFF_SUPPLY_CHAIN_SKILL_INPUT_REF": "payload://skill/current",
    "BFF_OPENCLAW_ENABLED": "true",
    "BFF_OPENCLAW_CONNECTOR_CREDENTIAL": "c" * 32,
    "TOOL_GATEWAY_JWT_KEY": "g" * 32,
}
CRM_ENV = {
    "BFF_CRM_OPENCLAW_ENABLED": "true",
    "BFF_CRM_OPENCLAW_TENANT_ID": "tenant-crm",
    "BFF_CRM_OPENCLAW_AGENT_ID": "crm",
    "BFF_CRM_OPENCLAW_INGRESS_URL": "http://127.0.0.1:18790/ebiz/tool-gateway/ingress",
    "BFF_CRM_OPENCLAW_INGRESS_CREDENTIAL": "s" * 32,
    "BFF_CRM_SERVICE_URL": "https://crm-service.example.com",
}


def load(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> BffSettings:
    for name in list(BASE_ENV) + list(CRM_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**BASE_ENV, **CRM_ENV, **overrides}.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    return BffSettings.from_environment()


def test_crm_profile_is_off_by_default_and_loads_when_fully_configured(monkeypatch) -> None:
    for name in CRM_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    off = BffSettings.from_environment()
    assert off.crm_openclaw_enabled is False
    assert off.crm_openclaw_agent_id == "crm"
    assert off.crm_openclaw_offer_id == "crm-case-advice"

    on = load(monkeypatch)
    assert on.crm_openclaw_enabled is True
    assert on.crm_openclaw_tenant_id == "tenant-crm"
    assert on.crm_openclaw_ingress_url == "http://127.0.0.1:18790/ebiz/tool-gateway/ingress"
    assert on.crm_openclaw_ingress_credential == "s" * 32
    assert on.crm_service_url == "https://crm-service.example.com"
    assert "s" * 32 not in repr(on)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"BFF_OPENCLAW_ENABLED": "false"}, "requires BFF_OPENCLAW_ENABLED"),
        ({"BFF_CRM_SERVICE_URL": None}, "BFF_CRM_SERVICE_URL is required"),
        ({"BFF_CRM_SERVICE_URL": "http://crm.internal:8080"}, "requires HTTPS"),
        ({"BFF_CRM_SERVICE_URL": "https://user:pw@crm.example.com"}, "credential-free"),
        ({"BFF_CRM_OPENCLAW_INGRESS_CREDENTIAL": "short"}, "at least 32"),
        ({"BFF_CRM_OPENCLAW_INGRESS_CREDENTIAL": None}, "at least 32"),
        (
            {"BFF_CRM_OPENCLAW_INGRESS_URL": "http://127.0.0.1:18789/ebiz/tool-gateway/ingress"},
            "CRM host instance",
        ),
        (
            {"BFF_CRM_OPENCLAW_INGRESS_URL": "http://10.0.0.5:18790/ebiz/tool-gateway/ingress"},
            "loopback",
        ),
        ({"BFF_CRM_OPENCLAW_AGENT_ID": "CRM Agent"}, "normalized OpenClaw agent ID"),
    ],
)
def test_crm_profile_fails_closed_on_incomplete_or_unsafe_configuration(
    monkeypatch, override, message
) -> None:
    with pytest.raises(ValueError, match=message):
        load(monkeypatch, **override)
