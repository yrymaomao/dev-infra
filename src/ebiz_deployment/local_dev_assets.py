"""Generate ignored, loopback-only configuration for ``LOCAL_DEV_E2E``."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from ebiz_runtime_contracts import canonical_json_bytes

from .local_dev_services import (
    BROKER_AUTH_TOKEN,
    CREDENTIAL_REF,
    OPENAI_API_KEY,
)

ERP_RECORD_DIGEST = "40d7affa6d0e17357303e3d85dd59e4779fc0eca8342bbb7947be5827b1c0d33"
MCP_RECORD_DIGEST = "fcee95cd892ece8684e812e5ae29c29fbf73f5c9c1e222b3e1d5ca4164fbe537"
OPENAI_RECORD_DIGEST = "76c114b222f6e1c41166332f37cb75e073c152575a033cbcf4e66f4baf6e3402"
SUPPLY_CHAIN_RECORD_DIGEST = "9cce1e64c58f20014e79bb688c91c4fe2914c92c2e59c29b356dfc191c1a2fae"

MODEL_SCHEMA_REF = (
    "schemas/supply-chain-inventory.schema.yaml#/$defs/SeasonalityAlignmentAnalysis"
)


@dataclass(frozen=True, slots=True)
class LocalDevAssets:
    root: Path
    deployment_config: Path
    plugin_policy: Path
    model_schemas: Path
    skill_root: Path
    environment: Path
    tls_ca: Path
    tls_cert: Path
    tls_key: Path


def _placeholder_evidence(tenant_id: str, snapshot_time: str) -> dict[str, Any]:
    digest = "0" * 64
    return {
        "evidence_id": "00000000-0000-4000-8000-000000000001",
        "tenant_id": tenant_id,
        "source_type": "approved_skill_input",
        "source_system": "local-dev-skill-store",
        "external_object_id": "SKU-LOCAL-1.json",
        "captured_at": snapshot_time,
        "freshness_at": snapshot_time,
        "content_ref": f"evidence://sha256/{digest}",
        "content_hash": digest,
        "content_type": "application/json",
        "size_bytes": 1,
        "access_level": "internal",
        "retention_policy": "operational_90_days",
        "schema_version": 1,
        "capability_ref": {
            "resource_type": "capability",
            "resource_id": "supply_chain.load_skill_input",
            "version": 1,
            "digest": "1" * 64,
        },
        "plugin_ref": {
            "resource_type": "plugin",
            "resource_id": "ebiz-agent-inventory-supply-chain",
            "version": "1.0.0",
            "digest": "2" * 64,
        },
        "execution_id": "00000000-0000-4000-8000-000000000002",
        "trace_id": "local-dev-skill-trace",
        "classification": "internal",
    }


def build_skill_document(
    *, snapshot_time: str, tenant_id: str, marketplace: str, sku: str
) -> dict[str, Any]:
    first_day = date.fromisoformat(snapshot_time[:10]) - timedelta(days=729)
    evidence = _placeholder_evidence(tenant_id, snapshot_time)
    document: dict[str, Any] = {
        "spec_version": "ebizhub.supply-chain-skill/v1",
        "scope": {"tenant_id": tenant_id, "marketplace": marketplace, "sku": sku},
        "observed_at": snapshot_time,
        "source": "local-dev-skill-store",
        "inputs": {
            "moq": {"value": 24, "unit": "pieces", "evidence_ref": evidence},
            "supplier_lead_time": {
                "value": 7,
                "unit": "days",
                "variance": 0.0,
                "evidence_ref": evidence,
            },
            "manufacturing_inventory": {
                "value": 0,
                "unit": "pieces",
                "status": "IN_PRODUCTION",
                "eta": (date.fromisoformat(snapshot_time[:10]) + timedelta(days=6)).isoformat(),
                "evidence_ref": evidence,
            },
            "package_spec": {"pack_size": 6, "unit": "pieces", "evidence_ref": evidence},
            "planning_policy": {
                "review_period_days": 7,
                "service_factor": 1.65,
                "minimum_gross_profit_ratio": 0.1,
                "maximum_coverage_days": 90,
                "evidence_ref": evidence,
            },
            "seasonality_profile": {
                "mode": "SEASONAL",
                "factors": [
                    {
                        "date": (first_day + timedelta(days=index)).isoformat(),
                        "factor": 1.0 + (index % 7) / 100,
                    }
                    for index in range(730)
                ],
                "maximum_relative_deviation": 0.2,
                "evidence_ref": evidence,
                "policy_version": "seasonality-policy-v1",
            },
        },
        "policy_version": "planning-policy-v1",
    }
    document["content_hash"] = hashlib.sha256(canonical_json_bytes(document)).hexdigest()
    return document


def _model_schemas() -> dict[str, Any]:
    return {
        MODEL_SCHEMA_REF: {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["assessment", "confidence", "explanation", "risk_flags"],
            "properties": {
                "assessment": {
                    "enum": ["ALIGNED", "MISALIGNED", "INSUFFICIENT_EVIDENCE"]
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "explanation": {"type": "string", "minLength": 1, "maxLength": 2000},
                "risk_flags": {
                    "type": "array",
                    "maxItems": 100,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
            },
        }
    }


def _plugin_policy(skill_root: Path) -> dict[str, Any]:
    return {
        "plugins": [
            {
                "plugin_id": "ebizhub.inventory-supply-chain",
                "version": "1.0.0",
                "package_name": "ebiz-agent-inventory-supply-chain",
                "entry_point": "inventory_supply_chain.plugin:factory",
                "package_digest": SUPPLY_CHAIN_RECORD_DIGEST,
                "permissions": [
                    "replenishment.preview",
                    "supply_chain.compute",
                    "supply_chain.preview",
                    "supply_chain.skill.read",
                ],
                "network_targets": [],
                "secret_names": [],
                "config": {"skill_root": str(skill_root)},
            }
        ]
    }


def _deployment_config(plugin_policy: Path) -> dict[str, Any]:
    network = {
        "timeout_seconds": 30,
        "connect_timeout_seconds": 10,
        "max_connections": 20,
        "verify_tls": True,
    }
    return {
        "schema_version": "1",
        "runtime": {
            "supported_api_version": "ebizhub.runtime/v1",
            "plugin_policy_path": str(plugin_policy),
        },
        "secrets": {
            "allowed_env": {
                "api_key": "DEPLOY_OPENAI_API_KEY",
                "broker_client_token": "DEPLOY_BROKER_CLIENT_TOKEN",
            }
        },
        "credential_broker": {
            "url": "http://127.0.0.1:18082/v1/resolve",
            "auth_secret_name": "broker_client_token",
            "allowed_provider_ids": ["mcp.streamable_http", "yeaher.erp"],
            "timeout_seconds": 5.0,
        },
        "base_ai_providers": [
            {
                "provider_id": "mcp.streamable_http",
                "package_name": "ebiz-adapter-mcp",
                "package_version": "0.1.0",
                "record_digest": "${MCP_RECORD_DIGEST}",
                "entry_point_group": "base_ai.provider_factories",
                "entry_point_value": "ebiz_adapter_mcp:McpProviderFactory",
                "api_version": "streamable-http/1",
                "enabled_operations": ["tools.call", "tools.list"],
                "egress_hosts": ["127.0.0.1"],
                "secret_names": [],
                "config": {
                    "server_name": "local-dev-inventory-read",
                    "url": "http://127.0.0.1:18081/mcp",
                    "allowed_tools": [
                        "query_inventory_by_warehouse",
                        "query_inventory_summary",
                        "query_purchase_in_transit_details",
                    ],
                    "network": network,
                },
            },
            {
                "provider_id": "yeaher.erp",
                "package_name": "ebiz-adapter-erp",
                "package_version": "0.1.0",
                "record_digest": "${ERP_RECORD_DIGEST}",
                "entry_point_group": "base_ai.provider_factories",
                "entry_point_value": "ebiz_adapter_erp:ErpProviderFactory",
                "api_version": "v1",
                "enabled_operations": [
                    "inventory.get_total_snapshot",
                    "sales_profit.get_sku_windows",
                ],
                "egress_hosts": ["127.0.0.1"],
                "secret_names": [],
                "config": {
                    "cockpit": {"base_url": "https://127.0.0.1:18080", "network": network},
                    "mcp": {
                        "tools": {"inventory.get_total_snapshot": "query_inventory_summary"}
                    },
                },
            },
            {
                "provider_id": "openai.responses",
                "package_name": "ebiz-adapter-model-openai",
                "package_version": "0.1.0",
                "record_digest": "${OPENAI_RECORD_DIGEST}",
                "entry_point_group": "base_ai.provider_factories",
                "entry_point_value": "ebiz_adapter_model_openai:OpenAIProviderFactory",
                "api_version": "responses/v1",
                "enabled_operations": ["responses.create_structured"],
                "egress_hosts": ["127.0.0.1"],
                "secret_names": ["api_key"],
                "config": {
                    "api_key_secret_name": "api_key",
                    "base_url": "https://127.0.0.1:18080/v1",
                    "enabled_operations": ["responses.create_structured"],
                    "network": network,
                },
            },
        ],
    }


def _field_allowlists() -> dict[str, dict[str, list[str]]]:
    return {
        "inventory-supply-chain.blocked-result-builder@1.0.0": {
            "supply_chain.build_blocked_result": [
                "decision", "policy_version", "run_id", "snapshot_time"
            ]
        },
        "inventory-supply-chain.classification-engine@1.0.0": {
            "supply_chain.classify_inventory": [
                "decision", "forecast", "inventory", "sales_profit",
                "seasonality_analysis", "skill_input",
            ]
        },
        "inventory-supply-chain.evidence-gate@1.0.0": {
            "supply_chain.evaluate_evidence": [
                "inventory", "sales_profit", "scope", "skill_input", "snapshot_time"
            ]
        },
        "inventory-supply-chain.forecast-engine@1.0.0": {
            "supply_chain.compute_forecast": [
                "decision", "inventory", "sales_profit", "seasonality_analysis", "skill_input"
            ]
        },
        "inventory-supply-chain.replenishment-engine@1.0.0": {
            "supply_chain.build_replenishment_proposal": [
                "classification", "decision", "forecast", "inventory", "sales_profit",
                "seasonality_analysis", "skill_input",
            ]
        },
        "inventory-supply-chain.result-builder@1.0.0": {
            "supply_chain.build_agent_result": [
                "classification", "decision", "forecast", "policy_version",
                "replenishment_preview", "run_id", "seasonality_analysis", "snapshot_time",
            ]
        },
        "inventory-supply-chain.scope-validator@1.0.0": {
            "supply_chain.prepare_scope": [
                "marketplace", "run_id", "skill_input_ref", "sku", "snapshot_time"
            ]
        },
        "inventory-supply-chain.skill-loader@1.0.0": {
            "supply_chain.load_skill_input": [
                "marketplace", "run_id", "skill_input_ref", "sku", "snapshot_time"
            ]
        },
        "yeaher.erp@0.1.0": {
            "inventory.get_total_snapshot": ["marketplace", "run_id", "sku", "snapshot_time"],
            "sales_profit.get_sku_windows": ["marketplace", "run_id", "sku", "snapshot_time"],
        },
    }


def _environment(assets: LocalDevAssets) -> dict[str, str]:
    targets = ",".join(sorted(_field_allowlists()))
    return {
        "LOCAL_DEV_E2E": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "APP_ENV": "local_dev",
        "EBIZ_DEPLOYMENT_CONFIG": str(assets.deployment_config),
        "RUNTIME_PLUGIN_POLICY_PATH": str(assets.plugin_policy),
        "APP_PLUGIN_POLICY_PATH": str(assets.plugin_policy),
        "MCP_RECORD_DIGEST": MCP_RECORD_DIGEST,
        "ERP_RECORD_DIGEST": ERP_RECORD_DIGEST,
        "OPENAI_RECORD_DIGEST": OPENAI_RECORD_DIGEST,
        "SUPPLY_CHAIN_RECORD_DIGEST": SUPPLY_CHAIN_RECORD_DIGEST,
        "MCP_PACKAGE_VERSION": "0.1.0",
        "ERP_PACKAGE_VERSION": "0.1.0",
        "OPENAI_ADAPTER_PACKAGE_VERSION": "0.1.0",
        "SUPPLY_CHAIN_PLUGIN_VERSION": "1.0.0",
        "CREDENTIAL_BROKER_URL": "http://127.0.0.1:18082/v1/resolve",
        "MCP_ENDPOINT": "http://127.0.0.1:18081/mcp",
        "MCP_EGRESS_HOST": "127.0.0.1",
        "COCKPIT_ENDPOINT": "https://127.0.0.1:18080",
        "COCKPIT_EGRESS_HOST": "127.0.0.1",
        "OPENAI_ENDPOINT": "https://127.0.0.1:18080/v1",
        "OPENAI_EGRESS_HOST": "127.0.0.1",
        "DEPLOY_BROKER_CLIENT_TOKEN": BROKER_AUTH_TOKEN,
        "DEPLOY_OPENAI_API_KEY": OPENAI_API_KEY,
        "LOCAL_DEV_TLS_CERT_FILE": str(assets.tls_cert),
        "LOCAL_DEV_TLS_KEY_FILE": str(assets.tls_key),
        "SSL_CERT_FILE": str(assets.tls_ca),
        "REQUESTS_CA_BUNDLE": str(assets.tls_ca),
        "SUPPLY_CHAIN_SKILL_ROOT": str(assets.skill_root),
        "APP_DATABASE_URL": (
            "postgresql+asyncpg://agent_runtime_test:agent_runtime_test@127.0.0.1:5433/agent_runtime_supply_chain_e2e_run5"
        ),
        "AGENT_RUNTIME_DATABASE_URL": (
            "postgresql+asyncpg://agent_runtime_test:agent_runtime_test@127.0.0.1:5433/agent_runtime_supply_chain_e2e_run5"
        ),
        "CHECKPOINT_DSN": (
            "postgresql://agent_runtime_test:agent_runtime_test@127.0.0.1:5433/agent_runtime_supply_chain_e2e_run5"
        ),
        "APP_JWT_SECRET": "local-dev-jwt-secret-at-least-thirty-two-characters",
        "APP_CONNECTOR_TARGETS": targets,
        "APP_CONNECTOR_FIELD_ALLOWLISTS": json.dumps(_field_allowlists(), separators=(",", ":")),
        "APP_CONNECTOR_RATE_BACKEND": "redis",
        "APP_REDIS_URL": "redis://127.0.0.1:6379/0",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "APP_MODEL_PROFILES": "structured",
        "APP_MODEL_ADAPTER_ID": "openai.responses",
        "APP_MODEL_ID": "local-dev-seasonality-model",
        "APP_MODEL_ADAPTER_VERSION": "0.1.0",
        "APP_MODEL_POLICY_VERSION": "local-dev-v1",
        "APP_MODEL_MAX_TIMEOUT_SECONDS": "60",
        "APP_MODEL_RESPONSE_SCHEMAS_PATH": str(assets.model_schemas),
        "APP_S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "APP_S3_BUCKET": "agent-runtime-live",
        "APP_S3_REGION": "us-east-1",
        "APP_S3_SUPPORTS_CONDITIONAL_DELETE": "true",
        "AWS_ACCESS_KEY_ID": "minioadmin",
        "AWS_SECRET_ACCESS_KEY": "minioadmin-ci-password",
        "AWS_EC2_METADATA_DISABLED": "true",
        "OTEL_SDK_DISABLED": "true",
        "SUPPLY_CHAIN_SMOKE_API_URL": "http://127.0.0.1:8000",
        "SUPPLY_CHAIN_TENANT_ID": "tenant-local-dev",
        "SUPPLY_CHAIN_CREDENTIAL_REF": CREDENTIAL_REF,
        "SUPPLY_CHAIN_MARKETPLACE": "US",
        "SUPPLY_CHAIN_SKU": "SKU-LOCAL-1",
        "SUPPLY_CHAIN_SKILL_INPUT_REF": "SKU-LOCAL-1.json",
        "SUPPLY_CHAIN_SNAPSHOT_TIME": "2026-08-24T00:00:00Z",
        "SUPPLY_CHAIN_EXPECTED_EVIDENCE_COUNT": "13",
    }


def write_local_dev_assets(root: Path) -> LocalDevAssets:
    root = root.resolve()
    skill_root = root / "skills"
    skill_root.mkdir(parents=True, exist_ok=True)
    assets = LocalDevAssets(
        root=root,
        deployment_config=root / "deployment.json",
        plugin_policy=root / "runtime-plugin-policy.json",
        model_schemas=root / "model-response-schemas.json",
        skill_root=skill_root,
        environment=root / "environment.json",
        tls_ca=root / "tls" / "local-dev-ca.pem",
        tls_cert=root / "tls" / "local-dev-server.pem",
        tls_key=root / "tls" / "local-dev-server-key.pem",
    )
    _write_local_tls(assets)
    snapshot = "2026-08-24T00:00:00Z"
    skill = build_skill_document(
        snapshot_time=snapshot,
        tenant_id="tenant-local-dev",
        marketplace="US",
        sku="SKU-LOCAL-1",
    )
    assets.skill_root.joinpath("SKU-LOCAL-1.json").write_text(
        json.dumps(skill, indent=2), encoding="utf-8"
    )
    assets.model_schemas.write_text(json.dumps(_model_schemas(), indent=2), encoding="utf-8")
    assets.plugin_policy.write_text(
        json.dumps(_plugin_policy(assets.skill_root), indent=2), encoding="utf-8"
    )
    assets.deployment_config.write_text(
        json.dumps(_deployment_config(assets.plugin_policy), indent=2), encoding="utf-8"
    )
    assets.environment.write_text(json.dumps(_environment(assets), indent=2), encoding="utf-8")
    return assets


def _write_local_tls(assets: LocalDevAssets) -> None:
    assets.tls_ca.parent.mkdir(parents=True, exist_ok=True)
    if assets.tls_ca.is_file() and assets.tls_cert.is_file() and assets.tls_key.is_file():
        return
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
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "eBizHub LOCAL_DEV_E2E loopback")]
    )
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
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
    root = Path(arguments[0]) if arguments else Path(".local")
    assets = write_local_dev_assets(root)
    print(json.dumps({"mode": "LOCAL_DEV_E2E", "root": str(assets.root)}, sort_keys=True))
    return 0


__all__ = ["LocalDevAssets", "build_skill_document", "main", "write_local_dev_assets"]
