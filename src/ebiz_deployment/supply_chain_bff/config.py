"""Versioned BFF runtime settings with secret-safe environment loading."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from .eta import EtaProfile

_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True, slots=True)
class BffSettings:
    database_url: str = field(repr=False)
    cursor_hmac_key: bytes = field(repr=False)
    jwt_secret: str = field(repr=False)
    runtime_url: str
    skill_input_ref: str
    runtime_credential_ref: str = field(repr=False)
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
    eta_profile: EtaProfile = field(
        default_factory=lambda: EtaProfile(
            version="supply-chain-v4-bootstrap-1",
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
        return cls(
            database_url=database_url,
            cursor_hmac_key=key,
            jwt_secret=jwt_secret,
            runtime_url=runtime_url,
            skill_input_ref=_required("BFF_SUPPLY_CHAIN_SKILL_INPUT_REF"),
            runtime_credential_ref=credential_ref,
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
            eta_profile=EtaProfile(
                version=os.environ.get("BFF_ETA_PROFILE_VERSION", "supply-chain-v4-bootstrap-1"),
                fixed_seconds=_float("BFF_ETA_FIXED_SECONDS", 2.0, minimum=0),
                per_item_seconds=_float("BFF_ETA_PER_ITEM_SECONDS", 15.0, minimum=0.001),
                concurrency=concurrency,
                uncertainty_ratio=_float("BFF_ETA_UNCERTAINTY_RATIO", 0.3, minimum=0, maximum=1),
            ),
        )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


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
