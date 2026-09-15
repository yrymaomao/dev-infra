"""Versioned BFF runtime settings with secret-safe environment loading."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from .eta import EtaProfile

_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_RABBIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_OPENCLAW_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class BffSettings:
    database_url: str = field(repr=False)
    cursor_hmac_key: bytes = field(repr=False)
    jwt_secret: str = field(repr=False)
    runtime_url: str
    skill_input_ref: str
    runtime_credential_ref: str = field(repr=False)
    rabbitmq_url: str | None = field(default=None, repr=False)
    snapshot_time_override: datetime | None = None
    max_batch_size: int = 200
    tenant_dispatch_concurrency: int = 4
    global_dispatch_concurrency: int = 32
    runtime_subscription_limit: int = 200
    db_tail_seconds: float = 0.5
    heartbeat_seconds: float = 15.0
    activity_push_per_second: int = 4
    runtime_event_retention_days: int = 7
    batch_mapping_retention_days: int = 30
    completed_activity_retention_days: int = 7
    async_start_enabled: bool = False
    stream_enabled: bool = False
    activity_ui_enabled: bool = False
    model_error_polish_enabled: bool = False
    legacy_batches_enabled: bool = True
    level2_enabled: bool = False
    level2_mq_enabled: bool = False
    autonomous_schedule_dispatch_enabled: bool = True
    openclaw_enabled: bool = False
    openclaw_reception_enabled: bool = False
    openclaw_connector_credential: str | None = field(default=None, repr=False)
    openclaw_ingress_url: str = "http://127.0.0.1:18789/ebiz/tool-gateway/ingress"
    openclaw_ingress_credential: str | None = field(default=None, repr=False)
    tool_gateway_jwt_key: str | None = field(default=None, repr=False)
    tool_gateway_issuer: str = "ebizhub-supply-chain-bff"
    tool_gateway_audience: str = "ebizhub-tool-gateway"
    openclaw_selector: str = "supply-chain-dev"
    openclaw_tenant_id: str = "tenant-local-dev"
    openclaw_principal_id: str = "openclaw-supply-chain"
    openclaw_agent_id: str = "main"
    openclaw_offer_id: str = "supply-chain-on-demand"
    # CRM reception profile (OpenClaw x CRM Phase 1). Its OpenClaw host is a
    # separate Adapter instance with its own ingress; the connector credential and
    # Gateway JWT key are the BFF's and shared with the Supply Chain profile.
    crm_openclaw_enabled: bool = False
    crm_openclaw_tenant_id: str = "tenant-local-dev"
    crm_openclaw_agent_id: str = "crm"
    crm_openclaw_offer_id: str = "crm-case-advice"
    crm_openclaw_ingress_url: str = "http://127.0.0.1:18790/ebiz/tool-gateway/ingress"
    crm_openclaw_ingress_credential: str | None = field(default=None, repr=False)
    crm_service_url: str | None = None
    # Phase 2 workbench operation routes (P2-G). Off by default: they need the
    # Runtime owner reads listed in docs/crm-profile.md "Workbench operation routes".
    crm_workbench_operations_enabled: bool = False
    max_selected_skus: int = 10_000
    bulk_batch_size: int = 200
    tenant_bulk_concurrency: int = 2
    global_bulk_concurrency: int = 8
    etl_wait_seconds: int = 1800
    etl_poll_seconds: int = 60
    report_schema_version: str = "supply-chain.report.v2"
    rabbitmq_exchange: str = "supply-chain.report.v1"
    rabbitmq_queue: str = "supply-chain.report-batch.v1"
    rabbitmq_routing_key: str = "supply-chain.report-batch.requested.v1"
    eta_profile: EtaProfile = field(
        default_factory=lambda: EtaProfile(
            version="supply-chain-v5-bootstrap-1",
            fixed_seconds=2.0,
            per_item_seconds=15.0,
            concurrency=4,
            uncertainty_ratio=0.3,
        )
    )

    @classmethod
    def from_environment(cls) -> BffSettings:
        database_url = _required("BFF_POSTGRESQL_URL")
        if make_url(database_url).drivername != "postgresql+asyncpg":
            raise ValueError("BFF_POSTGRESQL_URL must use postgresql+asyncpg")
        key = _required("BFF_CURSOR_HMAC_SIGNING_KEY").encode("utf-8")
        if len(key) < 32:
            raise ValueError("BFF_CURSOR_HMAC_SIGNING_KEY must contain at least 32 bytes")
        jwt_secret = _required("APP_JWT_SECRET")
        if len(jwt_secret) < 32:
            raise ValueError("APP_JWT_SECRET must contain at least 32 characters")
        runtime_url = _required("BFF_RUNTIME_URL").rstrip("/")
        parsed = urlsplit(runtime_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("BFF_RUNTIME_URL must be HTTP(S)")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("BFF_RUNTIME_URL requires HTTPS outside loopback")
        concurrency = _integer("BFF_TENANT_DISPATCH_CONCURRENCY", 4, minimum=1, maximum=64)
        credential_ref = _required("SUPPLY_CHAIN_CREDENTIAL_REF")
        if _CREDENTIAL_REF.fullmatch(credential_ref) is None:
            raise ValueError("SUPPLY_CHAIN_CREDENTIAL_REF must be an opaque bounded reference")
        level2_enabled = _boolean("BFF_LEVEL2_ENABLED", False)
        level2_mq_enabled = _boolean("BFF_LEVEL2_MQ_ENABLED", False)
        if level2_mq_enabled and not level2_enabled:
            raise ValueError("BFF_LEVEL2_MQ_ENABLED requires BFF_LEVEL2_ENABLED")
        rabbitmq_url = os.environ.get("BFF_RABBITMQ_URL", "").strip() or None
        if level2_mq_enabled and rabbitmq_url is None:
            raise ValueError("BFF_RABBITMQ_URL is required when Level 2 MQ is enabled")
        if rabbitmq_url is not None:
            rabbit = urlsplit(rabbitmq_url)
            if rabbit.scheme not in {"amqp", "amqps"} or not rabbit.netloc:
                raise ValueError("BFF_RABBITMQ_URL must use AMQP(S)")
            if rabbit.scheme == "amqp" and rabbit.hostname not in {"127.0.0.1", "localhost"}:
                raise ValueError("BFF_RABBITMQ_URL requires AMQPS outside loopback")
        openclaw_enabled = _boolean("BFF_OPENCLAW_ENABLED", False)
        openclaw_connector_credential = (
            os.environ.get("BFF_OPENCLAW_CONNECTOR_CREDENTIAL", "").strip() or None
        )
        tool_gateway_jwt_key = os.environ.get("TOOL_GATEWAY_JWT_KEY", "").strip() or None
        openclaw_ingress_url = _ingress_url(
            "BFF_OPENCLAW_INGRESS_URL", "http://127.0.0.1:18789/ebiz/tool-gateway/ingress"
        )
        crm_openclaw_enabled = _boolean("BFF_CRM_OPENCLAW_ENABLED", False)
        crm_openclaw_ingress_url = _ingress_url(
            "BFF_CRM_OPENCLAW_INGRESS_URL", "http://127.0.0.1:18790/ebiz/tool-gateway/ingress"
        )
        crm_openclaw_ingress_credential = (
            os.environ.get("BFF_CRM_OPENCLAW_INGRESS_CREDENTIAL", "").strip() or None
        )
        crm_service_url = os.environ.get("BFF_CRM_SERVICE_URL", "").strip() or None
        crm_workbench_operations_enabled = _boolean("BFF_CRM_WORKBENCH_OPERATIONS_ENABLED", False)
        if crm_workbench_operations_enabled and not crm_openclaw_enabled:
            raise ValueError(
                "BFF_CRM_WORKBENCH_OPERATIONS_ENABLED requires BFF_CRM_OPENCLAW_ENABLED "
                "(session exchange, tenant fence and profile)"
            )
        if crm_openclaw_enabled:
            if not openclaw_enabled:
                raise ValueError(
                    "BFF_CRM_OPENCLAW_ENABLED requires BFF_OPENCLAW_ENABLED "
                    "(shared connector credential and Gateway JWT key)"
                )
            if crm_service_url is None:
                raise ValueError(
                    "BFF_CRM_SERVICE_URL is required when the CRM reception is enabled"
                )
            _service_origin(crm_service_url, "BFF_CRM_SERVICE_URL")
            credential = crm_openclaw_ingress_credential
            if credential is None or len(credential) < 32:
                raise ValueError(
                    "BFF_CRM_OPENCLAW_INGRESS_CREDENTIAL must be a bounded credential of at least "
                    "32 characters"
                )
            if crm_openclaw_ingress_url == openclaw_ingress_url:
                raise ValueError(
                    "BFF_CRM_OPENCLAW_INGRESS_URL must name the CRM host instance, not the Supply "
                    "Chain one"
                )
        openclaw_ingress_credential = (
            os.environ.get("BFF_OPENCLAW_INGRESS_CREDENTIAL", "").strip() or None
        )
        if openclaw_enabled and (
            openclaw_connector_credential is None
            or len(openclaw_connector_credential) < 32
            or tool_gateway_jwt_key is None
            or len(tool_gateway_jwt_key) < 32
        ):
            raise ValueError("OpenClaw requires bounded connector and Gateway secrets")
        return cls(
            database_url=database_url,
            cursor_hmac_key=key,
            jwt_secret=jwt_secret,
            runtime_url=runtime_url,
            skill_input_ref=_required("BFF_SUPPLY_CHAIN_SKILL_INPUT_REF"),
            runtime_credential_ref=credential_ref,
            rabbitmq_url=rabbitmq_url,
            rabbitmq_exchange=_rabbit_name("BFF_RABBITMQ_EXCHANGE", "supply-chain.report.v1"),
            rabbitmq_queue=_rabbit_name("BFF_RABBITMQ_QUEUE", "supply-chain.report-batch.v1"),
            rabbitmq_routing_key=_rabbit_name(
                "BFF_RABBITMQ_ROUTING_KEY", "supply-chain.report-batch.requested.v1"
            ),
            snapshot_time_override=_optional_datetime("BFF_SUPPLY_CHAIN_SNAPSHOT_TIME"),
            tenant_dispatch_concurrency=concurrency,
            global_dispatch_concurrency=_integer(
                "BFF_GLOBAL_DISPATCH_CONCURRENCY", 32, minimum=1, maximum=512
            ),
            runtime_subscription_limit=_integer(
                "BFF_RUNTIME_SUBSCRIPTION_LIMIT", 200, minimum=1, maximum=200
            ),
            activity_push_per_second=_integer(
                "BFF_ACTIVITY_PUSH_PER_SECOND", 4, minimum=1, maximum=20
            ),
            async_start_enabled=_boolean("BFF_ASYNC_START_ENABLED", False),
            stream_enabled=_boolean("BFF_STREAM_ENABLED", False),
            activity_ui_enabled=_boolean("BFF_ACTIVITY_UI_ENABLED", False),
            model_error_polish_enabled=_boolean("BFF_MODEL_ERROR_POLISH_ENABLED", False),
            legacy_batches_enabled=_boolean("BFF_LEGACY_BATCHES_ENABLED", False),
            level2_enabled=level2_enabled,
            level2_mq_enabled=level2_mq_enabled,
            autonomous_schedule_dispatch_enabled=_boolean(
                "BFF_AUTONOMOUS_SCHEDULE_DISPATCH_ENABLED", True
            ),
            openclaw_enabled=openclaw_enabled,
            openclaw_connector_credential=openclaw_connector_credential,
            openclaw_ingress_url=openclaw_ingress_url,
            openclaw_ingress_credential=openclaw_ingress_credential,
            tool_gateway_jwt_key=tool_gateway_jwt_key,
            tool_gateway_issuer=_bounded("TOOL_GATEWAY_JWT_ISSUER", "ebizhub-supply-chain-bff"),
            tool_gateway_audience=_bounded("TOOL_GATEWAY_JWT_AUDIENCE", "ebizhub-tool-gateway"),
            openclaw_selector=_bounded("BFF_OPENCLAW_SELECTOR", "supply-chain-dev"),
            openclaw_tenant_id=_bounded("BFF_OPENCLAW_TENANT_ID", "tenant-local-dev"),
            openclaw_principal_id=_bounded("BFF_OPENCLAW_PRINCIPAL_ID", "openclaw-supply-chain"),
            openclaw_reception_enabled=_boolean("BFF_OPENCLAW_RECEPTION_ENABLED", False),
            openclaw_agent_id=_openclaw_agent_id("BFF_OPENCLAW_AGENT_ID", "main"),
            openclaw_offer_id=_bounded("BFF_OPENCLAW_OFFER_ID", "supply-chain-on-demand"),
            crm_openclaw_enabled=crm_openclaw_enabled,
            crm_openclaw_tenant_id=_bounded("BFF_CRM_OPENCLAW_TENANT_ID", "tenant-local-dev"),
            crm_openclaw_agent_id=_openclaw_agent_id("BFF_CRM_OPENCLAW_AGENT_ID", "crm"),
            crm_openclaw_offer_id=_bounded("BFF_CRM_OPENCLAW_OFFER_ID", "crm-case-advice"),
            crm_openclaw_ingress_url=crm_openclaw_ingress_url,
            crm_openclaw_ingress_credential=crm_openclaw_ingress_credential,
            crm_service_url=crm_service_url,
            crm_workbench_operations_enabled=crm_workbench_operations_enabled,
            max_selected_skus=_integer("BFF_MAX_SELECTED_SKUS", 10_000, minimum=1, maximum=10_000),
            bulk_batch_size=_integer("BFF_BULK_BATCH_SIZE", 200, minimum=1, maximum=200),
            tenant_bulk_concurrency=_integer(
                "BFF_TENANT_BULK_CONCURRENCY", 2, minimum=1, maximum=32
            ),
            global_bulk_concurrency=_integer(
                "BFF_GLOBAL_BULK_CONCURRENCY", 8, minimum=1, maximum=128
            ),
            etl_wait_seconds=_integer("BFF_ETL_WAIT_SECONDS", 1800, minimum=0, maximum=7200),
            etl_poll_seconds=_integer("BFF_ETL_POLL_SECONDS", 60, minimum=5, maximum=600),
            report_schema_version=_fixed(
                "BFF_REPORT_SCHEMA_VERSION",
                "supply-chain.report.v2",
            ),
            eta_profile=EtaProfile(
                version=os.environ.get("BFF_ETA_PROFILE_VERSION", "supply-chain-v5-bootstrap-1"),
                fixed_seconds=_float("BFF_ETA_FIXED_SECONDS", 2.0, minimum=0),
                per_item_seconds=_float("BFF_ETA_PER_ITEM_SECONDS", 15.0, minimum=0.001),
                concurrency=concurrency,
                uncertainty_ratio=_float("BFF_ETA_UNCERTAINTY_RATIO", 0.3, minimum=0, maximum=1),
            ),
        )


def _ingress_url(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/ebiz/tool-gateway/ingress"
    ):
        raise ValueError(f"{name} must be the loopback ingress path")
    return value


def _service_origin(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be a credential-free HTTP(S) origin")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(f"{name} requires HTTPS outside loopback")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _rabbit_name(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if _RABBIT_NAME.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded RabbitMQ name")
    return value


def _openclaw_agent_id(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if _OPENCLAW_AGENT_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a normalized OpenClaw agent ID")
    return value


def _optional_datetime(name: str) -> datetime | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an RFC3339 timestamp") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must carry a timezone")
    return value.astimezone(UTC)


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < minimum or value > maximum:
        raise ValueError(f"{name} is outside its supported range")
    return value


def _float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be numeric") from None
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside its supported range")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name, str(default)).strip().lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return raw == "true"


def _fixed(name: str, expected: str) -> str:
    value = os.environ.get(name, expected).strip()
    if value != expected:
        raise ValueError(f"{name} must be {expected}")
    return value


def _bounded(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value or len(value) > 256 or any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must be bounded text")
    return value
