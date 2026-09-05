from __future__ import annotations

import hashlib
import json

import pytest

from ebiz_deployment.config import (
    RuntimeGovernanceConfig,
    verify_runtime_governance_environment,
)


def governance() -> RuntimeGovernanceConfig:
    return RuntimeGovernanceConfig(
        config_revision=1,
        capacity_enforcement_enabled=False,
        registry_import_compiler_enabled=False,
        registry_import_publisher_enabled=False,
        capacity_registry_delegation_audience="ebizhub-capacity-registry",
        capacity_registry_trusted_proxy_cidrs=(),
        capacity_cache_ttl_seconds=60,
        capacity_max_concurrent_executions=64,
        capacity_max_graph_steps=200,
        capacity_max_model_calls=100,
        capacity_max_capability_calls=200,
        capacity_max_wall_seconds=7200,
        capacity_max_model_timeout_seconds=600,
        capacity_max_capability_timeout_seconds=300,
        capacity_max_total_model_attempts=10,
        capacity_monetary_model_budget_microusd=10**12,
    )


def environment() -> dict[str, str]:
    return {
        "APP_CAPACITY_ENFORCEMENT_ENABLED": "false",
        "APP_REGISTRY_IMPORT_COMPILER_ENABLED": "false",
        "APP_REGISTRY_IMPORT_PUBLISHER_ENABLED": "false",
        "APP_CAPACITY_REGISTRY_DELEGATION_AUDIENCE": "ebizhub-capacity-registry",
        "APP_CAPACITY_REGISTRY_TRUSTED_PROXY_CIDRS": "",
        "APP_CAPACITY_CACHE_TTL_SECONDS": "60",
        "APP_CAPACITY_MAX_CONCURRENT_EXECUTIONS": "64",
        "APP_CAPACITY_MAX_GRAPH_STEPS": "200",
        "APP_CAPACITY_MAX_MODEL_CALLS": "100",
        "APP_CAPACITY_MAX_CAPABILITY_CALLS": "200",
        "APP_CAPACITY_MAX_WALL_SECONDS": "7200",
        "APP_CAPACITY_MAX_MODEL_TIMEOUT_SECONDS": "600",
        "APP_CAPACITY_MAX_CAPABILITY_TIMEOUT_SECONDS": "300",
        "APP_CAPACITY_MAX_TOTAL_MODEL_ATTEMPTS": "10",
        "APP_CAPACITY_MONETARY_MODEL_BUDGET_MICROUSD": "1000000000000",
    }


def test_governance_digest_is_canonical_and_environment_matches() -> None:
    config = governance()
    expected = hashlib.sha256(
        json.dumps(
            config.model_dump(mode="json"),
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    assert config.canonical_digest == expected
    verify_runtime_governance_environment(config, environment())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_CAPACITY_ENFORCEMENT_ENABLED", "yes"),
        ("APP_REGISTRY_IMPORT_PUBLISHER_ENABLED", "true"),
        ("APP_CAPACITY_CACHE_TTL_SECONDS", "0"),
        ("APP_CAPACITY_MAX_MODEL_CALLS", "99"),
    ],
)
def test_governance_environment_missing_invalid_or_drifted_fails_closed(
    name: str, value: str
) -> None:
    values = environment()
    values[name] = value

    with pytest.raises(ValueError, match=name):
        verify_runtime_governance_environment(governance(), values)

    del values[name]
    with pytest.raises(ValueError, match=name):
        verify_runtime_governance_environment(governance(), values)
