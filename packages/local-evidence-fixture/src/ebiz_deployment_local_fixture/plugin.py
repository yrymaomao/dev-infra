"""Inert, network-free Provider for deterministic-local evidence provenance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from agent_runtime.connectors.contracts import ConnectorInvocation, ConnectorResponse
from agent_runtime.plugins.contracts import ProviderKind, ProviderRegistration
from agent_runtime.plugins.manifest import HealthCheckSpec, PluginManifest

PLUGIN_ID = "deployment.fixture.governed-artifact"
PLUGIN_VERSION = "1.0.0"
PACKAGE_NAME = "ebiz-deployment-local-evidence-fixture"
ENTRY_POINT = "ebiz_deployment_local_fixture.plugin:factory"
PROVIDER_ID = PLUGIN_ID


class LocalEvidenceFixtureConnector:
    """Echo only a hash; the seed CLI, not this Connector, writes governed bytes."""

    async def invoke(self, invocation: ConnectorInvocation) -> ConnectorResponse:
        if invocation.operation != "deployment.fixture.seed_governed_artifact":
            raise ValueError("local fixture accepts only its declared operation")
        digest = invocation.payload.get("content_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("content_sha256 must be a SHA-256 string")
        return ConnectorResponse(
            output={"content_sha256": digest},
            source_system="deployment-local-evidence-fixture",
            external_object_id=digest,
            captured_at=datetime(2026, 8, 24, tzinfo=UTC),
            duration_ms=0,
        )

    async def health_check(self) -> bool:
        return True


MANIFEST = PluginManifest(
    plugin_id=PLUGIN_ID,
    version=PLUGIN_VERSION,
    api_version="ebizhub.runtime/v1",
    package_name=PACKAGE_NAME,
    package_digest="0" * 64,
    provider_kinds=(ProviderKind.CONNECTOR,),
    entry_point=ENTRY_POINT,
    permissions=("deployment.fixture.read",),
    network_allowlist=(),
    secret_names=(),
    config_schema={"type": "object", "additionalProperties": False},
    health_check=HealthCheckSpec(timeout_seconds=1),
)

REGISTRATION = ProviderRegistration(
    provider_id=PROVIDER_ID,
    kind=ProviderKind.CONNECTOR,
    provider=LocalEvidenceFixtureConnector(),
    permissions=frozenset({"deployment.fixture.read"}),
)


class _Factory:
    manifest = MANIFEST

    @staticmethod
    def validate_config(config: Mapping[str, Any]) -> None:
        if config:
            raise ValueError("local fixture accepts no configuration")

    @staticmethod
    def create(config: Mapping[str, Any]) -> Sequence[ProviderRegistration]:
        _Factory.validate_config(config)
        return (REGISTRATION,)


factory = _Factory()

