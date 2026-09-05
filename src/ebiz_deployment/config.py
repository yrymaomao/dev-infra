"""Strict, secret-safe deployment configuration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from ipaddress import ip_network
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_runtime.plugins.manifest import PluginHostPolicy
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

_ENV_REFERENCE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_ENTRY_POINT_GROUP = "base_ai.provider_factories"
_READ_TOOLS = (
    "query_inventory_summary_v2",
    "query_sku_boston_cohort_v1",
    "query_sku_sales_profit_windows_v1",
    "query_sku_upc_mapping",
)
_EXPECTED_PROVIDERS: dict[str, dict[str, object]] = {
    "mcp.streamable_http": {
        "package_name": "ebiz-adapter-mcp",
        "package_version": "0.1.0",
        "entry_point_value": "ebiz_adapter_mcp:McpProviderFactory",
        "api_version": "streamable-http/1",
        "enabled_operations": ("tools.call", "tools.list"),
    },
    "yeaher.erp": {
        "package_name": "ebiz-adapter-erp",
        "package_version": "0.1.0",
        "entry_point_value": "ebiz_adapter_erp:ErpProviderFactory",
        "api_version": "v1",
        "enabled_operations": (
            "catalog.resolve_sku_identity",
            "inventory.get_total_snapshot",
            "sales_profit.get_boston_cohort",
            "sales_profit.get_sku_windows",
        ),
    },
    "openai.responses": {
        "package_name": "ebiz-adapter-model-openai",
        "package_version": "0.1.1",
        "entry_point_value": "ebiz_adapter_model_openai:OpenAIProviderFactory",
        "api_version": "responses/v1",
        "enabled_operations": ("responses.create_structured",),
    },
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeGovernanceConfig(StrictModel):
    config_revision: int = Field(ge=1)
    capacity_enforcement_enabled: bool
    registry_import_compiler_enabled: bool
    registry_import_publisher_enabled: bool
    capacity_registry_delegation_audience: str = Field(min_length=1, max_length=128)
    capacity_registry_trusted_proxy_cidrs: tuple[str, ...]
    capacity_cache_ttl_seconds: int = Field(ge=1, le=3_600)
    capacity_max_concurrent_executions: int = Field(ge=1, le=10_000)
    capacity_max_graph_steps: int = Field(ge=1, le=200)
    capacity_max_model_calls: int = Field(ge=1, le=100)
    capacity_max_capability_calls: int = Field(ge=1, le=200)
    capacity_max_wall_seconds: int = Field(ge=1, le=7_200)
    capacity_max_model_timeout_seconds: int = Field(ge=1, le=3_600)
    capacity_max_capability_timeout_seconds: int = Field(ge=1, le=3_600)
    capacity_max_total_model_attempts: int = Field(ge=1, le=100)
    capacity_monetary_model_budget_microusd: int = Field(ge=0, le=10**18)

    @field_validator("capacity_registry_trusted_proxy_cidrs")
    @classmethod
    def validate_unique_proxy_cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            normalized = tuple(str(ip_network(item, strict=True)) for item in value)
        except ValueError as error:
            raise ValueError("capacity registry trusted proxy CIDRs are invalid") from error
        if len(normalized) != len(set(normalized)):
            raise ValueError("capacity registry trusted proxy CIDRs must be unique and non-empty")
        return tuple(sorted(normalized))

    @property
    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class RuntimeConfig(StrictModel):
    supported_api_version: str = Field(min_length=1, max_length=128)
    plugin_policy_path: Path
    governance: RuntimeGovernanceConfig


class SecretEnvironmentConfig(StrictModel):
    allowed_env: dict[str, str] = Field(min_length=1)

    @field_validator("allowed_env")
    @classmethod
    def validate_names(cls, value: dict[str, str]) -> dict[str, str]:
        symbol = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
        environment = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
        if any(not symbol.fullmatch(name) for name in value):
            raise ValueError("allowed_env contains an invalid symbolic secret name")
        if any(not environment.fullmatch(name) for name in value.values()):
            raise ValueError("allowed_env contains an invalid environment variable name")
        if len(set(value.values())) != len(value):
            raise ValueError("allowed_env environment variable names must be unique")
        return dict(sorted(value.items()))


class CredentialBrokerConfig(StrictModel):
    url: str = Field(min_length=1, max_length=2048)
    auth_secret_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    allowed_provider_ids: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, le=30)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        _require_https_or_loopback(value, "credential_broker.url")
        return value

    @field_validator("allowed_provider_ids")
    @classmethod
    def validate_provider_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        expected = ("mcp.streamable_http", "yeaher.erp")
        if tuple(sorted(value)) != expected:
            raise ValueError("allowed_provider_ids must contain only the request-auth providers")
        return tuple(sorted(value))


class ProviderDeploymentConfig(StrictModel):
    provider_id: str = Field(min_length=1, max_length=128)
    package_name: str = Field(min_length=1, max_length=128)
    package_version: str = Field(min_length=1, max_length=128)
    record_digest: str
    entry_point_group: str = Field(min_length=1, max_length=128)
    entry_point_value: str = Field(min_length=3, max_length=256)
    api_version: str = Field(min_length=1, max_length=128)
    enabled_operations: tuple[str, ...] = Field(min_length=1)
    egress_hosts: tuple[str, ...]
    secret_names: tuple[str, ...]
    config: dict[str, JsonValue]

    @property
    def package_digest(self) -> str:
        return self.record_digest

    @field_validator("record_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("record_digest must be a lowercase SHA-256 attestation")
        return value

    @field_validator("enabled_operations", "egress_hosts", "secret_names")
    @classmethod
    def validate_unique_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_known_read_only_provider(self) -> ProviderDeploymentConfig:
        expected = _EXPECTED_PROVIDERS.get(self.provider_id)
        if expected is None:
            raise ValueError("provider_id is not part of the approved production composition")
        identity = {
            "package_name": self.package_name,
            "package_version": self.package_version,
            "entry_point_value": self.entry_point_value,
            "api_version": self.api_version,
            "enabled_operations": self.enabled_operations,
        }
        if identity != expected or self.entry_point_group != _ENTRY_POINT_GROUP:
            raise ValueError(
                "provider pin or enabled_operations differs from the approved read-only surface"
            )
        _validate_provider_config(self)
        return self


class CapabilitySetPin(StrictModel):
    set_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    distribution_name: str = Field(min_length=1, max_length=128)
    distribution_version: str = Field(min_length=1, max_length=128)
    record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


_EXPECTED_CAPABILITY_SETS = {
    "inventory.core": (2, "ebiz-capability-inventory-catalog", "2.0.0"),
    "commerce-sales.analytics": (2, "ebiz-capability-commerce-sales-catalog", "2.0.0"),
    "supply-chain.planning": (2, "ebiz-capability-supply-chain", "2.0.0"),
}
_EXPECTED_PLANNING_PROVIDERS = {
    "supply-chain-planning.action-router",
    "supply-chain-planning.classification-engine",
    "supply-chain-planning.clearance-engine",
    "supply-chain-planning.forecast-engine",
    "supply-chain-planning.fulfillment-resolver",
    "supply-chain-planning.replenishment-engine",
}


class SupplyChainReleaseConfig(StrictModel):
    agent_id: str = Field(pattern=r"^inventory-supply-chain$")
    agent_version: int = Field(ge=4, le=4)
    agent_distribution: str = Field(pattern=r"^ebiz-agent-inventory-supply-chain$")
    agent_distribution_version: str = Field(pattern=r"^4\.0\.0$")
    agent_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    workflow_code: str = Field(pattern=r"^inventory-supply-chain-daily$")
    workflow_version: int = Field(ge=4, le=4)
    workflow_artifact_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability_sets: tuple[CapabilitySetPin, ...] = Field(min_length=3, max_length=3)
    provider_versions: dict[str, str] = Field(min_length=7, max_length=7)

    @field_validator("capability_sets")
    @classmethod
    def validate_capability_sets(
        cls, value: tuple[CapabilitySetPin, ...]
    ) -> tuple[CapabilitySetPin, ...]:
        by_id = {item.set_id: item for item in value}
        if len(by_id) != len(value) or set(by_id) != set(_EXPECTED_CAPABILITY_SETS):
            raise ValueError("capability_sets must be the exact three Supply Chain v4 sets")
        for set_id, (
            version,
            distribution,
            distribution_version,
        ) in _EXPECTED_CAPABILITY_SETS.items():
            item = by_id[set_id]
            if (item.version, item.distribution_name, item.distribution_version) != (
                version,
                distribution,
                distribution_version,
            ):
                raise ValueError("capability set identity differs from the immutable v4 release")
        return tuple(sorted(value, key=lambda item: item.set_id))

    @field_validator("provider_versions")
    @classmethod
    def validate_provider_versions(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != {"yeaher.erp", *_EXPECTED_PLANNING_PROVIDERS}:
            raise ValueError("provider_versions must contain the exact v4 provider pins")
        if value["yeaher.erp"] != "0.1.0" or any(
            value[provider] != "2.0.0" for provider in _EXPECTED_PLANNING_PROVIDERS
        ):
            raise ValueError("provider_versions differ from the reviewed v4 wheels")
        return dict(sorted(value.items()))


class DeploymentCompositionConfig(StrictModel):
    schema_version: str = Field(pattern=r"^2$")
    runtime: RuntimeConfig
    secrets: SecretEnvironmentConfig
    credential_broker: CredentialBrokerConfig
    base_ai_providers: tuple[ProviderDeploymentConfig, ...] = Field(min_length=3, max_length=3)
    supply_chain_release: SupplyChainReleaseConfig
    runtime_plugin_policy: PluginHostPolicy | None = Field(default=None, exclude=True)

    @field_validator("base_ai_providers")
    @classmethod
    def validate_provider_set(
        cls, value: tuple[ProviderDeploymentConfig, ...]
    ) -> tuple[ProviderDeploymentConfig, ...]:
        if {item.provider_id for item in value} != set(_EXPECTED_PROVIDERS):
            raise ValueError("base_ai_providers must contain the exact three production providers")
        return tuple(sorted(value, key=lambda item: item.provider_id))

    @model_validator(mode="after")
    def validate_secret_slots(self) -> DeploymentCompositionConfig:
        configured = set(self.secrets.allowed_env)
        required = {self.credential_broker.auth_secret_name}
        for provider in self.base_ai_providers:
            required.update(provider.secret_names)
        if required - configured:
            raise ValueError("all broker and provider secret names must be in secrets.allowed_env")
        return self


def load_deployment_config(
    path: Path | str,
    environ: Mapping[str, str],
) -> DeploymentCompositionConfig:
    """Load deployment and independent Runtime plugin policy without leaking values."""

    config_path = Path(path).resolve()
    document = _read_json(config_path, "deployment config")
    expanded = _expand_environment(document, environ)
    try:
        config = DeploymentCompositionConfig.model_validate(expanded)
    except ValidationError as error:
        raise ValueError(_safe_validation_message("deployment config", error)) from None
    policy_path = config.runtime.plugin_policy_path.resolve()
    if not policy_path.is_file():
        raise ValueError("runtime.plugin_policy_path must name a readable file")
    policy_document = _expand_environment(_read_json(policy_path, "Runtime plugin policy"), environ)
    try:
        policy = PluginHostPolicy.model_validate_json(json.dumps(policy_document))
    except ValidationError as error:
        raise ValueError(_safe_validation_message("Runtime plugin policy", error)) from None
    allow_local_fixture = (
        environ.get("APP_ENV", "").strip() == "local_dev"
        and environ.get("LOCAL_DEV_E2E", "").strip().lower() == "true"
    )
    _validate_supply_chain_policy(policy, allow_local_fixture=allow_local_fixture)
    return config.model_copy(update={"runtime_plugin_policy": policy})


_RUNTIME_GOVERNANCE_ENVIRONMENT = {
    "capacity_enforcement_enabled": "APP_CAPACITY_ENFORCEMENT_ENABLED",
    "registry_import_compiler_enabled": "APP_REGISTRY_IMPORT_COMPILER_ENABLED",
    "registry_import_publisher_enabled": "APP_REGISTRY_IMPORT_PUBLISHER_ENABLED",
    "capacity_registry_delegation_audience": "APP_CAPACITY_REGISTRY_DELEGATION_AUDIENCE",
    "capacity_registry_trusted_proxy_cidrs": "APP_CAPACITY_REGISTRY_TRUSTED_PROXY_CIDRS",
    "capacity_cache_ttl_seconds": "APP_CAPACITY_CACHE_TTL_SECONDS",
    "capacity_max_concurrent_executions": "APP_CAPACITY_MAX_CONCURRENT_EXECUTIONS",
    "capacity_max_graph_steps": "APP_CAPACITY_MAX_GRAPH_STEPS",
    "capacity_max_model_calls": "APP_CAPACITY_MAX_MODEL_CALLS",
    "capacity_max_capability_calls": "APP_CAPACITY_MAX_CAPABILITY_CALLS",
    "capacity_max_wall_seconds": "APP_CAPACITY_MAX_WALL_SECONDS",
    "capacity_max_model_timeout_seconds": "APP_CAPACITY_MAX_MODEL_TIMEOUT_SECONDS",
    "capacity_max_capability_timeout_seconds": "APP_CAPACITY_MAX_CAPABILITY_TIMEOUT_SECONDS",
    "capacity_max_total_model_attempts": "APP_CAPACITY_MAX_TOTAL_MODEL_ATTEMPTS",
    "capacity_monetary_model_budget_microusd": "APP_CAPACITY_MONETARY_MODEL_BUDGET_MICROUSD",
}


def verify_runtime_governance_environment(
    config: RuntimeGovernanceConfig,
    environ: Mapping[str, str],
) -> None:
    """Fail before Runtime start if process settings differ from reviewed configuration."""

    for field_name, environment_name in _RUNTIME_GOVERNANCE_ENVIRONMENT.items():
        raw = environ.get(environment_name)
        if raw is None:
            raise ValueError(f"{environment_name} is required by runtime governance")
        expected = getattr(config, field_name)
        normalized = raw.strip()
        if isinstance(expected, bool):
            if normalized.lower() not in {"true", "false"}:
                raise ValueError(f"{environment_name} must be true or false")
            actual: object = normalized.lower() == "true"
        elif isinstance(expected, int):
            if re.fullmatch(r"0|[1-9][0-9]*", normalized) is None:
                raise ValueError(f"{environment_name} must be a canonical non-negative integer")
            actual = int(normalized)
        elif isinstance(expected, tuple):
            actual = tuple(sorted(item.strip() for item in normalized.split(",") if item.strip()))
        else:
            actual = normalized
        if actual != expected:
            raise ValueError(f"{environment_name} differs from reviewed runtime governance")


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError(f"{label} is missing or malformed") from None


def _expand_environment(value: object, environ: Mapping[str, str]) -> object:
    if isinstance(value, dict):
        return {key: _expand_environment(item, environ) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item, environ) for item in value]
    if isinstance(value, str):
        match = _ENV_REFERENCE.fullmatch(value)
        if match is None:
            return value
        name = match.group(1)
        resolved = environ.get(name, "").strip()
        if not resolved:
            raise ValueError(f"required environment variable {name} is missing")
        return resolved
    return value


def _validate_supply_chain_policy(
    policy: PluginHostPolicy, *, allow_local_fixture: bool = False
) -> None:
    by_id = {item.plugin_id: item for item in policy.plugins}
    expected_ids = {"supply-chain-planning"}
    if allow_local_fixture:
        expected_ids.add("deployment.fixture.governed-artifact")
    if len(by_id) != len(policy.plugins) or set(by_id) != expected_ids:
        raise ValueError("Runtime plugin policy contains an unapproved plugin set")
    plugin = by_id["supply-chain-planning"]
    expected_permissions = frozenset({"supply_chain.preview"})
    if (
        plugin.plugin_id != "supply-chain-planning"
        or plugin.version != "2.0.0"
        or plugin.package_name != "ebiz-capability-supply-chain"
        or plugin.entry_point != "ebiz_capability_supply_chain.plugin:factory"
        or plugin.permissions != expected_permissions
        or plugin.network_targets
        or plugin.secret_names
        or plugin.config
    ):
        raise ValueError("Runtime plugin policy is not the exact read-only Supply Chain pin")
    if allow_local_fixture:
        fixture = by_id["deployment.fixture.governed-artifact"]
        if (
            fixture.version != "1.0.0"
            or fixture.package_name != "ebiz-deployment-local-evidence-fixture"
            or fixture.entry_point != "ebiz_deployment_local_fixture.plugin:factory"
            or fixture.permissions != frozenset({"deployment.fixture.read"})
            or fixture.network_targets
            or fixture.secret_names
            or fixture.config
        ):
            raise ValueError("Runtime local fixture policy is not the exact deterministic pin")


def _validate_provider_config(provider: ProviderDeploymentConfig) -> None:
    config = provider.config
    if provider.provider_id == "mcp.streamable_http":
        if set(config) != {"server_name", "url", "allowed_tools", "auth_profile", "network"}:
            raise ValueError("MCP config fields are incomplete or unknown")
        allowed_tools = config.get("allowed_tools")
        if not isinstance(allowed_tools, list) or tuple(allowed_tools) != _READ_TOOLS:
            raise ValueError("allowed_tools must be the exact Supply Chain v4 read tools")
        if config.get("auth_profile") != "X_MCP_KEY":
            raise ValueError("MCP auth_profile must be X_MCP_KEY")
        _validate_endpoint_host(config.get("url"), provider.egress_hosts, "MCP")
        return
    if provider.provider_id == "yeaher.erp":
        if set(config) != {"mcp"}:
            raise ValueError("ERP config must contain only the fixed MCP bindings")
        mcp = config.get("mcp")
        expected = {
            "tools": {
                "catalog.resolve_sku_identity": "query_sku_upc_mapping",
                "inventory.get_total_snapshot": "query_inventory_summary_v2",
                "sales_profit.get_boston_cohort": "query_sku_boston_cohort_v1",
                "sales_profit.get_sku_windows": "query_sku_sales_profit_windows_v1",
            }
        }
        if mcp != expected or provider.egress_hosts:
            raise ValueError("ERP MCP bindings must be the fixed Supply Chain v4 tool map")
        return
    if set(config) != {"api_key_secret_name", "base_url", "enabled_operations", "network"}:
        raise ValueError(
            "OpenAI config must contain key name, endpoint, enabled_operations, and network limits"
        )
    # The symbolic secret name matches the adapter manifest's declared secret
    # slot ("api_key"); the composition layer verifies that equality at start.
    if config.get("api_key_secret_name") != "api_key":
        raise ValueError("OpenAI model secret must use the approved symbolic name")
    if config.get("enabled_operations") != ["responses.create_structured"]:
        raise ValueError("OpenAI config must enable only responses.create_structured")
    if provider.secret_names != ("api_key",):
        raise ValueError("OpenAI secret_names must contain only api_key")
    _validate_endpoint_host(config.get("base_url"), provider.egress_hosts, "OpenAI")


def _validate_endpoint_host(value: object, hosts: tuple[str, ...], label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} endpoint is required")
    parsed = _require_https_or_loopback(value, f"{label} endpoint")
    if parsed.hostname is None or hosts != (parsed.hostname,):
        raise ValueError(f"{label} egress_hosts must exactly match the endpoint host")


def _require_https_or_loopback(value: str, label: str) -> Any:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None or not parsed.netloc:
        raise ValueError(f"{label} must be a credential-free HTTP(S) URL")
    if parsed.scheme == "https":
        return parsed
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
        return parsed
    raise ValueError(f"{label} must use HTTPS")


def _safe_validation_message(label: str, error: ValidationError) -> str:
    fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
    reasons = sorted({str(item["msg"]) for item in error.errors()})
    return f"{label} invalid at: {', '.join(fields)} ({'; '.join(reasons)})"


__all__ = [
    "CredentialBrokerConfig",
    "DeploymentCompositionConfig",
    "ProviderDeploymentConfig",
    "RuntimeGovernanceConfig",
    "SupplyChainReleaseConfig",
    "load_deployment_config",
    "verify_runtime_governance_environment",
]
