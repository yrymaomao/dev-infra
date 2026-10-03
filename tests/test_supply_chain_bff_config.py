from __future__ import annotations

import pytest

from ebiz_deployment.supply_chain_bff.config import BffSettings


def _required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "BFF_POSTGRESQL_URL",
        "postgresql+asyncpg://service:secret@127.0.0.1/supply_chain_bff",
    )
    monkeypatch.setenv("BFF_CURSOR_HMAC_SIGNING_KEY", "c" * 32)
    monkeypatch.setenv("APP_JWT_SECRET", "j" * 32)
    monkeypatch.setenv("BFF_RUNTIME_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("BFF_SUPPLY_CHAIN_SKILL_INPUT_REF", "payload://skill/current")
    monkeypatch.setenv("SUPPLY_CHAIN_CREDENTIAL_REF", "opaque:runtime-service")


def _user_bound_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFF_SUPPLY_CHAIN_USER_BOUND_CREDENTIALS_ENABLED", "true")
    monkeypatch.setenv("MCP_USER_REF_SIGNING_KEY", "s" * 32)


def test_level2_flags_default_off_and_product_limits_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_environment(monkeypatch)
    settings = BffSettings.from_environment()
    assert settings.legacy_batches_enabled is False
    assert settings.level2_enabled is False
    assert settings.level2_mq_enabled is False
    assert settings.rabbitmq_url is None
    assert settings.max_selected_skus == 10_000
    assert settings.bulk_batch_size == 200
    assert settings.tenant_bulk_concurrency == 2
    assert settings.global_bulk_concurrency == 8
    assert settings.etl_wait_seconds == 1800
    assert settings.etl_poll_seconds == 60
    assert settings.report_schema_version == "supply-chain.report.v2"
    assert settings.user_bound_credentials_enabled is False


def test_user_bound_mode_requires_erp_and_signing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("BFF_SUPPLY_CHAIN_USER_BOUND_CREDENTIALS_ENABLED", "true")
    monkeypatch.delenv("BFF_ERP_SESSION_REDIS_URL_FILE")
    with pytest.raises(ValueError, match="BFF_ERP_SESSION_REDIS_URL_FILE"):
        BffSettings.from_environment()
    monkeypatch.setenv("BFF_ERP_SESSION_REDIS_URL_FILE", str(tmp_path / "redis-url"))
    with pytest.raises(ValueError, match="MCP_USER_REF_SIGNING_KEY"):
        BffSettings.from_environment()
    monkeypatch.setenv("MCP_USER_REF_SIGNING_KEY", "s" * 32)
    settings = BffSettings.from_environment()
    assert settings.user_bound_credentials_enabled is True
    assert settings.user_mcp_ref_codec is not None
    assert (
        settings.user_mcp_ref_codec.parse(
            settings.user_mcp_ref_codec.issue("tenant-a", "alice"),
            expected_tenant_id="tenant-a",
        )
        == "alice"
    )


def test_active_supply_chain_refuses_static_userless_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("BFF_LEVEL2_ENABLED", "true")
    with pytest.raises(ValueError, match="user-bound credentials"):
        BffSettings.from_environment()


def test_report_schema_version_rejects_v1_for_new_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("BFF_REPORT_SCHEMA_VERSION", "supply-chain.report.v1")

    with pytest.raises(ValueError, match="must be supply-chain.report.v2"):
        BffSettings.from_environment()


def test_report_schema_version_rejects_unknown_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("BFF_REPORT_SCHEMA_VERSION", "supply-chain.report.v3")

    with pytest.raises(ValueError, match="must be supply-chain.report.v2"):
        BffSettings.from_environment()


def test_legacy_batches_can_be_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("BFF_LEGACY_BATCHES_ENABLED", "true")

    settings = BffSettings.from_environment()

    assert settings.legacy_batches_enabled is True


def test_level2_mq_requires_parent_flag_and_queue_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("BFF_LEVEL2_MQ_ENABLED", "true")
    with pytest.raises(ValueError, match="requires BFF_LEVEL2_ENABLED"):
        BffSettings.from_environment()

    monkeypatch.setenv("BFF_LEVEL2_ENABLED", "true")
    with pytest.raises(ValueError, match="BFF_RABBITMQ_URL is required"):
        BffSettings.from_environment()


def test_external_rabbitmq_must_use_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("BFF_LEVEL2_ENABLED", "true")
    monkeypatch.setenv("BFF_LEVEL2_MQ_ENABLED", "true")
    monkeypatch.setenv("BFF_RABBITMQ_URL", "amqp://service:secret@rabbit.internal/vhost")
    with pytest.raises(ValueError, match="requires AMQPS"):
        BffSettings.from_environment()

    monkeypatch.setenv("BFF_RABBITMQ_URL", "amqps://service:secret@rabbit.internal/vhost")
    _user_bound_environment(monkeypatch)
    settings = BffSettings.from_environment()
    assert settings.level2_enabled is True
    assert settings.level2_mq_enabled is True
    assert settings.rabbitmq_url is not None


def test_level2_mq_names_are_explicitly_loaded_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv("BFF_LEVEL2_ENABLED", "true")
    monkeypatch.setenv("BFF_LEVEL2_MQ_ENABLED", "true")
    monkeypatch.setenv("BFF_RABBITMQ_URL", "amqp://service:secret@127.0.0.1/")
    monkeypatch.setenv("BFF_RABBITMQ_EXCHANGE", "supply-chain.report.dev-r1")
    monkeypatch.setenv("BFF_RABBITMQ_QUEUE", "supply-chain.report-batch.dev-r1")
    monkeypatch.setenv("BFF_RABBITMQ_ROUTING_KEY", "supply-chain.report-batch.requested.dev-r1")
    _user_bound_environment(monkeypatch)

    settings = BffSettings.from_environment()

    assert settings.rabbitmq_exchange == "supply-chain.report.dev-r1"
    assert settings.rabbitmq_queue == "supply-chain.report-batch.dev-r1"
    assert settings.rabbitmq_routing_key == "supply-chain.report-batch.requested.dev-r1"

    monkeypatch.setenv("BFF_RABBITMQ_QUEUE", "invalid/name")
    with pytest.raises(ValueError, match="BFF_RABBITMQ_QUEUE must be a bounded RabbitMQ name"):
        BffSettings.from_environment()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BFF_MAX_SELECTED_SKUS", "10001"),
        ("BFF_BULK_BATCH_SIZE", "201"),
        ("BFF_TENANT_BULK_CONCURRENCY", "0"),
        ("BFF_GLOBAL_BULK_CONCURRENCY", "129"),
        ("BFF_ETL_WAIT_SECONDS", "7201"),
        ("BFF_ETL_POLL_SECONDS", "4"),
    ],
)
def test_level2_environment_rejects_values_outside_product_bounds(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    _required_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="outside its supported range"):
        BffSettings.from_environment()


@pytest.fixture(autouse=True)
def erp_secret_files(monkeypatch, tmp_path):
    redis_file = tmp_path / "redis-url"
    jwt_file = tmp_path / "jwt-key"
    redis_file.write_text("redis://127.0.0.1:6379/2", encoding="utf-8")
    jwt_file.write_text("synthetic-erp-signing-key", encoding="utf-8")
    monkeypatch.setenv("BFF_ERP_SESSION_REDIS_URL_FILE", str(redis_file))
    monkeypatch.setenv("BFF_ERP_SESSION_JWT_SECRET_FILE", str(jwt_file))
