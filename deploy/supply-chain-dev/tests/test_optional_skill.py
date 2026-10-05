"""v2-only startup must not require a legacy single-SKU Skill artifact."""

import os

import pytest

from ebiz_deployment.supply_chain_bff.config import BffSettings


@pytest.fixture
def environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("BFF_", "APP_", "MCP_", "SUPPLY_CHAIN_", "TOOL_GATEWAY_")):
            monkeypatch.delenv(key)
    for key, value in {
        "BFF_POSTGRESQL_URL": "postgresql+asyncpg://fixture@localhost/fixture",
        "BFF_CURSOR_HMAC_SIGNING_KEY": "fixture-only-signing-key-32-bytes-long",
        "APP_JWT_SECRET": "fixture-only-jwt-key-at-least-32-bytes",
        "BFF_RUNTIME_URL": "http://127.0.0.1:8013",
        "BFF_LEGACY_BATCHES_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_disabled_legacy_batches_need_no_skill(environment):
    settings = BffSettings.from_environment()
    assert settings.skill_input_ref == ""
    assert settings.legacy_batches_enabled is False


def test_legacy_batches_still_require_skill(environment):
    environment.setenv("BFF_LEGACY_BATCHES_ENABLED", "true")
    with pytest.raises(ValueError, match="BFF_SUPPLY_CHAIN_SKILL_INPUT_REF is required"):
        BffSettings.from_environment()


def test_explicit_legacy_skill_is_preserved(environment):
    environment.setenv("BFF_LEGACY_BATCHES_ENABLED", "true")
    environment.setenv("BFF_SUPPLY_CHAIN_SKILL_INPUT_REF", "reviewed-artifact-reference")
    assert BffSettings.from_environment().skill_input_ref == "reviewed-artifact-reference"


def test_user_bound_v2_uses_optional_skill(environment, tmp_path):
    redis = tmp_path / "erp-redis-url"
    secret = tmp_path / "erp-jwt-secret"
    redis.write_text("redis://localhost:6379/0", encoding="utf-8")
    secret.write_text("fixture-only-erp-jwt", encoding="utf-8")
    environment.setenv("BFF_ERP_SESSION_REDIS_URL_FILE", str(redis))
    environment.setenv("BFF_ERP_SESSION_JWT_SECRET_FILE", str(secret))
    environment.setenv("MCP_USER_REF_SIGNING_KEY", "fixture-only-user-ref-at-least-32-bytes")
    environment.setenv("BFF_LEVEL2_ENABLED", "true")
    environment.setenv("BFF_SUPPLY_CHAIN_USER_BOUND_CREDENTIALS_ENABLED", "true")
    settings = BffSettings.from_environment()
    assert settings.level2_enabled and settings.user_bound_credentials_enabled
    assert settings.skill_input_ref == ""
    assert settings.erp_session is not None
