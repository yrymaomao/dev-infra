"""Generate ignored, loopback-only assets for deterministic Supply Chain v6 tests."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, cast

import yaml
from base_ai.providers import discover_provider_factory_descriptors
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .attestation import Descriptor, build_attestation_environment
from .config import DeploymentCompositionConfig
from .local_dev_services import BROKER_AUTH_TOKEN, CREDENTIAL_REF, OPENAI_API_KEY
from .record_attestation import attest_installed_distribution
from .release import write_base_ai_provider_attestation

MODEL_SCHEMA_REF = "schemas/seasonality-analysis.schema.yaml"
_CONNECTOR_FIELD_ALLOWLISTS = {
    "yeaher.erp@0.2.0": {
        "catalog.resolve_sku_identity": ["market_scope", "sku", "snapshot_time"],
        "inventory.get_total_snapshot": [
            "fulfillment_mode",
            "market_scope",
            "sku",
            "snapshot_time",
        ],
        "inventory.get_batch_snapshot": ["market_scope", "skus"],
        "sales_profit.get_boston_cohort": ["market_scope", "snapshot_time", "target_sku"],
        "sales_profit.get_sku_windows": ["market_scope", "sku", "snapshot_time"],
    },
    "supply-chain-planning.fulfillment-resolver@3.0.0": {
        "supply_chain.resolve_fulfillment_mode": ["fulfillment_mode"]
    },
    "supply-chain-planning.forecast-engine@3.0.0": {
        "supply_chain.compute_forecast": [
            "production_lead_time_days",
            "review_period_days",
            "seasonality_profile",
            "snapshot_time",
            "windows",
        ]
    },
    "supply-chain-planning.classification-engine@3.0.0": {
        "supply_chain.classify_inventory": [
            "cohort",
            "cohort_total_eligible",
            "forecast",
            "growth_ratio",
            "growth_unavailable_reason",
            "seasonality_profile",
            "snapshot_time",
        ]
    },
    "supply-chain-planning.action-router@3.0.0": {
        "supply_chain.route_inventory_action": [
            "available_quantity",
            "classification",
            "evidence_decision",
            "forecast",
            "planning_policy",
            "purchase_in_transit_quantity",
            "sales_windows",
        ]
    },
    "supply-chain-planning.replenishment-engine@3.0.0": {
        "supply_chain.build_replenishment_proposal": [
            "available_quantity",
            "forecast",
            "planning_policy",
            "purchase_in_transit_quantity",
        ]
    },
    "supply-chain-planning.clearance-engine@3.0.0": {
        "supply_chain.build_clearance_proposal": [
            "available_quantity",
            "planning_policy",
            "purchase_in_transit_quantity",
            "route",
        ]
    },
    "supply-chain-planning.level2-fulfillment@3.0.0": {
        "supply_chain.resolve_fulfillment_mode": [
            "csv_mode",
            "csv_mixed_ratio",
            "policy_rule",
            "weekly_channel_sales",
            "fba_data_available",
            "fbm_data_available",
        ]
    },
    "supply-chain-planning.level2-forecast@3.0.0": {
        "supply_chain.compute_forecast": [
            "sku",
            "weekly_history",
            "data_cutoff",
            "feature_snapshot_ref",
            "future_signals",
            "cohort_prior",
            "optional_signal_available",
            "data_snapshot_lagged",
        ]
    },
    "supply-chain-planning.level2-optimizer@3.0.0": {
        "supply_chain.optimize_inventory_action": [
            "forecast",
            "available_quantity",
            "inbound_quantity",
            "unit_cost",
            "current_unit_price",
            "fulfillment_mode",
            "mixed_ratio",
            "policy_rule",
            "as_of",
            "markdown_grid",
        ]
    },
    "supply-chain-planning.level2-batch@3.0.0": {
        "supply_chain.plan_batch": [
            "report_run_id",
            "batch_id",
            "item_offset",
            "data_cutoff",
            "selection_snapshot_ref",
            "selection_rows",
            "policy_rules",
            "identity",
            "fbm_inventory",
            "fba_inventory",
            "sales",
        ]
    },
}


@dataclass(frozen=True, slots=True)
class LocalDevAssets:
    root: Path
    deployment_config: Path
    plugin_policy: Path
    base_ai_attestation: Path
    fixture_plugin_policy: Path
    fixture_contract_root: Path
    model_schemas: Path
    skill_root: Path
    environment: Path
    tls_ca: Path
    tls_cert: Path
    tls_key: Path


def build_skill_document(
    *, snapshot_time: str, tenant_id: str, market_scope: str, sku: str
) -> dict[str, Any]:
    """Return deterministic rollback policy data; Runtime binds scope and hash metadata."""

    del snapshot_time, tenant_id, market_scope, sku
    return {
        "policy_version": "local-dev-v5",
        "planning_policy": {
            "moq": 24,
            "pack_size": 6,
            "production_lead_time_days": 42,
            "review_period_days": 7,
            "service_factor": 1.65,
            "maximum_coverage_days": 90,
            "minimum_gross_margin_ratio": 0.1,
            "clearance_enabled": True,
        },
        "seasonality_profile": {
            "mode": "SEASONAL",
            "monthly_indices": [{"month": month, "index": 1.0} for month in range(1, 13)],
        },
    }


def _model_schemas() -> dict[str, Any]:
    distribution = metadata.distribution("ebiz-agent-inventory-supply-chain")
    if distribution.version != "4.1.0":
        raise ValueError("installed Supply Chain Agent version is unavailable")
    schema_path = Path(
        str(
            distribution.locate_file(
                "inventory_supply_chain_agent/schemas/seasonality-analysis.schema.yaml"
            )
        )
    ).resolve(strict=True)
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise ValueError("installed Supply Chain model schema is invalid")
    return {MODEL_SCHEMA_REF: schema}


def _plugin_policy(
    digests: dict[str, str], *, fixture_record_digest: str | None = None
) -> dict[str, Any]:
    plugins = [
        {
            "plugin_id": "supply-chain-planning",
            "version": "3.0.0",
            "package_name": "ebiz-capability-supply-chain",
            "entry_point": "ebiz_capability_supply_chain.plugin:factory",
            "package_digest": digests["SUPPLY_CHAIN_PLANNING_RECORD_DIGEST"],
            "permissions": ["supply_chain.preview"],
            "network_targets": [],
            "secret_names": [],
            "config": {},
        }
    ]
    if fixture_record_digest is not None:
        plugins.append(
            {
                "plugin_id": "deployment.fixture.governed-artifact",
                "version": "1.0.1",
                "package_name": "ebiz-deployment-local-evidence-fixture",
                "entry_point": "ebiz_deployment_local_fixture.plugin:factory",
                "package_digest": fixture_record_digest,
                "permissions": ["deployment.fixture.read"],
                "network_targets": [],
                "secret_names": [],
                "config": {},
            }
        )
    return {"plugins": plugins}


def _streaming_bff_release() -> dict[str, Any]:
    return {
        "version": "0.1.4",
        "schema": "supply_chain_bff",
        "migration_head": "0003_batch_selection_payload",
        "secret_references": [
            "supply_chain_bff_postgresql_url",
            "supply_chain_cursor_hmac_signing_key",
            "supply_chain_bff_rabbitmq_url",
        ],
        "features": {
            "async_start": False,
            "runtime_stream": False,
            "activity_ui": False,
            "model_error_polish": False,
            "level2": False,
            "level2_mq": False,
        },
        "limits": {
            "max_batch_size": 200,
            "tenant_dispatch_concurrency": 4,
            "runtime_subscription_limit": 200,
            "max_selected_skus": 10000,
            "bulk_batch_size": 200,
            "tenant_bulk_concurrency": 2,
            "global_bulk_concurrency": 8,
        },
        "etl_wait": {
            "max_seconds": 1800,
            "poll_seconds": 60,
            "timeout_risk_flag": "DATA_SNAPSHOT_LAGGED",
        },
        "schedule_defaults": {
            "weekday": 1,
            "local_time": "12:00",
            "policy_mode": "ACTIVE_AT_RUN",
        },
        "stream": {
            "db_tail_ms": 500,
            "heartbeat_seconds": 15,
            "activity_push_per_second": 4,
        },
        "retention_days": {
            "runtime_events": 7,
            "batch_mapping": 30,
            "completed_activity": 7,
        },
        "activity_schema": "business-agent.activity-event.v1",
        "eta_profile": {
            "version": "supply-chain-v6-level2-1",
            "fixed_seconds": 2.0,
            "per_item_seconds": 15.0,
            "uncertainty_ratio": 0.3,
        },
    }


def _release(digests: dict[str, str]) -> dict[str, Any]:
    return {
        "agent_id": "inventory-supply-chain",
        "agent_version": 6,
        "agent_distribution": "ebiz-agent-inventory-supply-chain",
        "agent_distribution_version": "4.1.0",
        "agent_record_digest": digests["SUPPLY_CHAIN_AGENT_RECORD_DIGEST"],
        "workflow_code": "inventory-supply-chain-daily",
        "workflow_version": 6,
        "workflow_artifact_digest": digests["SUPPLY_CHAIN_WORKFLOW_DIGEST"],
        "capability_sets": [
            {
                "set_id": "inventory.core",
                "version": 4,
                "distribution_name": "ebiz-capability-inventory-catalog",
                "distribution_version": "4.0.0",
                "record_digest": digests["INVENTORY_CATALOG_RECORD_DIGEST"],
            },
            {
                "set_id": "commerce-sales.analytics",
                "version": 4,
                "distribution_name": "ebiz-capability-commerce-sales-catalog",
                "distribution_version": "4.0.0",
                "record_digest": digests["COMMERCE_SALES_CATALOG_RECORD_DIGEST"],
            },
            {
                "set_id": "supply-chain.planning",
                "version": 3,
                "distribution_name": "ebiz-capability-supply-chain",
                "distribution_version": "3.0.0",
                "record_digest": digests["SUPPLY_CHAIN_PLANNING_RECORD_DIGEST"],
            },
        ],
        "streaming_bff": _streaming_bff_release(),
        "provider_versions": {
            "yeaher.erp": "0.2.0",
            "supply-chain-planning.fulfillment-resolver": "3.0.0",
            "supply-chain-planning.forecast-engine": "3.0.0",
            "supply-chain-planning.classification-engine": "3.0.0",
            "supply-chain-planning.action-router": "3.0.0",
            "supply-chain-planning.replenishment-engine": "3.0.0",
            "supply-chain-planning.clearance-engine": "3.0.0",
            "supply-chain-planning.level2-fulfillment": "3.0.0",
            "supply-chain-planning.level2-forecast": "3.0.0",
            "supply-chain-planning.level2-optimizer": "3.0.0",
            "supply-chain-planning.level2-batch": "3.0.0",
        },
    }


def _deployment_config(plugin_policy: Path, digests: dict[str, str]) -> dict[str, Any]:
    network = {
        "timeout_seconds": 30,
        "connect_timeout_seconds": 10,
        "max_connections": 20,
        "verify_tls": True,
    }
    return {
        "schema_version": "2",
        "runtime": {
            "supported_api_version": "ebizhub.runtime/v1",
            "plugin_policy_path": str(plugin_policy),
        },
        "secrets": {
            "allowed_env": {
                "api_key": "DEPLOY_OPENAI_API_KEY",
                "broker_client_token": "DEPLOY_BROKER_CLIENT_TOKEN",
                "supply_chain_bff_postgresql_url": "BFF_POSTGRESQL_URL",
                "supply_chain_cursor_hmac_signing_key": "BFF_CURSOR_HMAC_SIGNING_KEY",
                "supply_chain_bff_rabbitmq_url": "BFF_RABBITMQ_URL",
            }
        },
        "credential_broker": {
            "url": "http://127.0.0.1:18082/v1/resolve",
            "auth_secret_name": "broker_client_token",
            "allowed_provider_ids": ["mcp.streamable_http", "yeaher.erp"],
            "timeout_seconds": 5.0,
        },
        "base_ai_providers": [
            _mcp_provider(network, digests),
            _erp_provider(digests),
            _model_provider(network, digests),
        ],
        "supply_chain_release": _release(digests),
    }


def _mcp_provider(network: dict[str, Any], digests: dict[str, str]) -> dict[str, Any]:
    return {
        "provider_id": "mcp.streamable_http",
        "package_name": "ebiz-adapter-mcp",
        "package_version": "0.1.0",
        "record_digest": digests["MCP_RECORD_DIGEST"],
        "entry_point_group": "base_ai.provider_factories",
        "entry_point_value": "ebiz_adapter_mcp:McpProviderFactory",
        "api_version": "streamable-http/1",
        "enabled_operations": ["tools.call", "tools.list"],
        "egress_hosts": ["127.0.0.1"],
        "secret_names": [],
        "config": {
            "server_name": "local-dev-erp-read",
            "url": "http://127.0.0.1:18081/mcp",
            "allowed_tools": [
                "query_inventory_batch_snapshot_v1",
                "query_fba_inventory_snapshot_v1",
                "query_inventory_skus_by_threshold_v1",
                "query_inventory_summary_v2",
                "query_sku_boston_cohort_v1",
                "query_sku_fulfillment_sales_profit_windows_v2",
                "query_sku_identity_mapping_v1",
                "query_sku_sales_profit_windows_v1",
                "query_sku_upc_mapping",
            ],
            "auth_profile": "X_MCP_KEY",
            "network": network,
        },
    }


def _erp_provider(digests: dict[str, str]) -> dict[str, Any]:
    return {
        "provider_id": "yeaher.erp",
        "package_name": "ebiz-adapter-erp",
        "package_version": "0.2.0",
        "record_digest": digests["ERP_RECORD_DIGEST"],
        "entry_point_group": "base_ai.provider_factories",
        "entry_point_value": "ebiz_adapter_erp:ErpProviderFactory",
        "api_version": "v1",
        "enabled_operations": [
            "catalog.resolve_sku_identity",
            "inventory.get_total_snapshot",
            "inventory.get_batch_snapshot",
            "sales_profit.get_boston_cohort",
            "sales_profit.get_sku_windows",
            "inventory.list_skus_by_threshold",
            "catalog.resolve_sku_identity_batch",
            "inventory.get_fba_snapshot",
            "sales_profit.get_sku_fulfillment_windows",
        ],
        "egress_hosts": [],
        "secret_names": [],
        "config": {
            "mcp": {
                "tools": {
                    "catalog.resolve_sku_identity": "query_sku_upc_mapping",
                    "inventory.get_total_snapshot": "query_inventory_summary_v2",
                    "sales_profit.get_boston_cohort": "query_sku_boston_cohort_v1",
                    "sales_profit.get_sku_windows": "query_sku_sales_profit_windows_v1",
                    "inventory.list_skus_by_threshold": "query_inventory_skus_by_threshold_v1",
                    "inventory.get_batch_snapshot": "query_inventory_batch_snapshot_v1",
                    "catalog.resolve_sku_identity_batch": "query_sku_identity_mapping_v1",
                    "inventory.get_fba_snapshot": "query_fba_inventory_snapshot_v1",
                    "sales_profit.get_sku_fulfillment_windows": (
                        "query_sku_fulfillment_sales_profit_windows_v2"
                    ),
                }
            }
        },
    }


def _model_provider(network: dict[str, Any], digests: dict[str, str]) -> dict[str, Any]:
    return {
        "provider_id": "openai.responses",
        "package_name": "ebiz-adapter-model-openai",
        "package_version": "0.1.1",
        "record_digest": digests["OPENAI_RECORD_DIGEST"],
        "entry_point_group": "base_ai.provider_factories",
        "entry_point_value": "ebiz_adapter_model_openai:OpenAIProviderFactory",
        "api_version": "responses/v1",
        "enabled_operations": ["responses.create_structured"],
        "egress_hosts": ["localhost"],
        "secret_names": ["api_key"],
        "config": {
            "api_key_secret_name": "api_key",
            "base_url": "https://localhost:18080/v1",
            "enabled_operations": ["responses.create_structured"],
            "network": network,
        },
    }


def _environment(
    assets: LocalDevAssets, digests: dict[str, str], *, snapshot_time: str
) -> dict[str, str]:
    return {
        "LOCAL_DEV_E2E": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "APP_ENV": "local_dev",
        "APP_CONNECTOR_TARGETS": ",".join(sorted(_CONNECTOR_FIELD_ALLOWLISTS)),
        "APP_CONNECTOR_FIELD_ALLOWLISTS": json.dumps(
            _CONNECTOR_FIELD_ALLOWLISTS, sort_keys=True, separators=(",", ":")
        ),
        "EBIZ_DEPLOYMENT_CONFIG": str(assets.deployment_config),
        "RUNTIME_PLUGIN_POLICY_PATH": str(assets.plugin_policy),
        "APP_PLUGIN_POLICY_PATH": str(assets.plugin_policy),
        "BASE_AI_PROVIDER_ATTESTATION_PATH": str(assets.base_ai_attestation),
        "LOCAL_FIXTURE_PLUGIN_POLICY_PATH": str(assets.fixture_plugin_policy),
        "LOCAL_FIXTURE_CONTRACT_ROOT": str(assets.fixture_contract_root),
        **digests,
        "MCP_PACKAGE_VERSION": "0.1.0",
        "ERP_PACKAGE_VERSION": "0.2.0",
        "OPENAI_ADAPTER_PACKAGE_VERSION": "0.1.1",
        "CREDENTIAL_BROKER_URL": "http://127.0.0.1:18082/v1/resolve",
        "MCP_ENDPOINT": "http://127.0.0.1:18081/mcp",
        "MCP_EGRESS_HOST": "127.0.0.1",
        "OPENAI_ENDPOINT": "https://localhost:18080/v1",
        "OPENAI_EGRESS_HOST": "localhost",
        "DEPLOY_BROKER_CLIENT_TOKEN": BROKER_AUTH_TOKEN,
        "DEPLOY_OPENAI_API_KEY": OPENAI_API_KEY,
        "LOCAL_DEV_TLS_CERT_FILE": str(assets.tls_cert),
        "LOCAL_DEV_TLS_KEY_FILE": str(assets.tls_key),
        "SSL_CERT_FILE": str(assets.tls_ca),
        "REQUESTS_CA_BUNDLE": str(assets.tls_ca),
        "SUPPLY_CHAIN_SMOKE_API_URL": "http://127.0.0.1:8000",
        "SUPPLY_CHAIN_TENANT_ID": "tenant-local-dev",
        "SUPPLY_CHAIN_CREDENTIAL_REF": CREDENTIAL_REF,
        "SUPPLY_CHAIN_MARKET_SCOPE": "NA_COMPANY",
        "SUPPLY_CHAIN_SKU": "SKU-LOCAL-1",
        # Populate only after the Skill bytes below are stored through Runtime's
        # governed payload/evidence seam. A file path is never an artifact ref.
        "SUPPLY_CHAIN_SKILL_INPUT_REF": "",
        "SUPPLY_CHAIN_SKILL_FILE": str(assets.skill_root / "SKU-LOCAL-1.json"),
        "SUPPLY_CHAIN_SKILL_FILE_SHA256": hashlib.sha256(
            (assets.skill_root / "SKU-LOCAL-1.json").read_bytes()
        ).hexdigest(),
        "SUPPLY_CHAIN_SNAPSHOT_TIME": snapshot_time,
        "SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT": "5",
        "SUPPLY_CHAIN_EXPECTED_RESULT_STATUS": "COMPLETE",
        "SUPPLY_CHAIN_RUN_ID": "supply-chain-v5-local-1",
    }


def write_local_dev_assets(
    root: Path,
    *,
    fixture_search_paths: Sequence[Path] | None = None,
    snapshot_time: datetime | None = None,
) -> LocalDevAssets:
    root = root.resolve()
    current_snapshot = snapshot_time or datetime.now(UTC)
    if current_snapshot.tzinfo is None or current_snapshot.utcoffset() is None:
        raise ValueError("snapshot_time must be timezone-aware")
    canonical_snapshot = (
        current_snapshot.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    skill_root = root / "skills"
    skill_root.mkdir(parents=True, exist_ok=True)
    fixture_attestation = attest_installed_distribution(
        distribution_name="ebiz-deployment-local-evidence-fixture",
        distribution_version="1.0.1",
        entry_point_group="ebiz_agents.providers",
        entry_point_name="deployment.fixture.governed-artifact",
        entry_point_value="ebiz_deployment_local_fixture.plugin:factory",
        search_paths=fixture_search_paths,
    )
    if fixture_search_paths is None:
        fixture_distribution = metadata.distribution("ebiz-deployment-local-evidence-fixture")
    else:
        candidates = tuple(
            item
            for item in metadata.distributions(
                path=[str(path.resolve()) for path in fixture_search_paths]
            )
            if item.metadata["Name"] == "ebiz-deployment-local-evidence-fixture"
        )
        if len(candidates) != 1:
            raise ValueError("local evidence fixture distribution must be installed exactly once")
        fixture_distribution = candidates[0]
    fixture_contract_root = Path(
        str(fixture_distribution.locate_file("ebiz_deployment_local_fixture/contracts"))
    ).resolve()
    assets = LocalDevAssets(
        root=root,
        deployment_config=root / "deployment.json",
        plugin_policy=root / "runtime-plugin-policy.json",
        base_ai_attestation=root / "base-ai-provider-attestation.json",
        fixture_plugin_policy=root / "local-fixture-plugin-policy.json",
        fixture_contract_root=fixture_contract_root,
        model_schemas=root / "model-response-schemas.json",
        skill_root=skill_root,
        environment=root / "environment.json",
        tls_ca=root / "tls/local-dev-ca.pem",
        tls_cert=root / "tls/local-dev-server.pem",
        tls_key=root / "tls/local-dev-server-key.pem",
    )
    _write_local_tls(assets)
    digests = _installed_digests()
    skill = build_skill_document(
        snapshot_time=canonical_snapshot,
        tenant_id="tenant-local-dev",
        market_scope="NA_COMPANY",
        sku="SKU-LOCAL-1",
    )
    assets.skill_root.joinpath("SKU-LOCAL-1.json").write_text(
        json.dumps(skill, indent=2), encoding="utf-8"
    )
    assets.model_schemas.write_text(json.dumps(_model_schemas(), indent=2), encoding="utf-8")
    local_policy = _plugin_policy(
        digests, fixture_record_digest=fixture_attestation.canonical_digest
    )
    assets.plugin_policy.write_text(json.dumps(local_policy, indent=2), encoding="utf-8")
    assets.fixture_plugin_policy.write_text(
        json.dumps(local_policy, indent=2),
        encoding="utf-8",
    )
    deployment_document = _deployment_config(assets.plugin_policy, digests)
    assets.deployment_config.write_text(json.dumps(deployment_document, indent=2), encoding="utf-8")
    write_base_ai_provider_attestation(
        DeploymentCompositionConfig.model_validate(deployment_document),
        assets.base_ai_attestation,
    )
    assets.environment.write_text(
        json.dumps(_environment(assets, digests, snapshot_time=canonical_snapshot), indent=2),
        encoding="utf-8",
    )
    return assets


def _installed_digests() -> dict[str, str]:
    descriptors = cast(Sequence[Descriptor], discover_provider_factory_descriptors())
    digests = build_attestation_environment(descriptors)
    distribution = metadata.distribution("ebiz-agent-inventory-supply-chain")
    workflow = Path(
        str(
            distribution.locate_file(
                "inventory_supply_chain_agent/workflows/inventory-supply-chain-daily.yaml"
            )
        )
    )
    digests["SUPPLY_CHAIN_WORKFLOW_DIGEST"] = hashlib.sha256(workflow.read_bytes()).hexdigest()
    return digests


def _write_local_tls(assets: LocalDevAssets) -> None:
    assets.tls_ca.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "eBizHub LOCAL_DEV_E2E CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    assets.tls_ca.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    assets.tls_cert.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    assets.tls_key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print("usage: ebiz-local-dev-assets [output-directory]", file=sys.stderr)
        return 2
    assets = write_local_dev_assets(Path(arguments[0]) if arguments else Path(".local"))
    print(json.dumps({"mode": "LOCAL_DEV_E2E", "root": str(assets.root)}, sort_keys=True))
    return 0


__all__ = ["LocalDevAssets", "build_skill_document", "main", "write_local_dev_assets"]
