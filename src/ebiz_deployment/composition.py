"""Construct the deployment-owned Base AI composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType

import httpx
from agent_runtime.application.provider_composition import (
    ManagedProviderComposition,
    ProviderCompositionRoot,
)
from agent_runtime.base_ai.composition import BaseAICompositionRoot, BaseAIProviderDeployment
from agent_runtime.plugins.contracts import PluginSnapshot
from agent_runtime.plugins.manifest import AllowedPlugin, PluginHostPolicy
from base_ai.providers import ProviderBootstrapContext

from .config import DeploymentCompositionConfig
from .credentials import EnvironmentSecretResolver
from .service_discovery import NacosServiceResolver, create_nacos_client
from .user_mcp_credentials import McpUserKeyResolver, UserMcpRefCodec


@dataclass(slots=True)
class _UserKeyComposition:
    snapshot: PluginSnapshot
    _base: ManagedProviderComposition
    _client: httpx.AsyncClient
    _closed: bool = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._base.aclose()
        finally:
            await self._client.aclose()


class UserKeyCompositionRoot:
    """Acquire the Key-query connection pool only when Runtime starts providers."""

    def __init__(self, build: Callable[[httpx.AsyncClient], BaseAICompositionRoot]) -> None:
        self._build = build

    async def start(self) -> _UserKeyComposition:
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        try:
            base = await self._build(client).start()
        except BaseException:
            await client.aclose()
            raise
        return _UserKeyComposition(base.snapshot, base, client)


@dataclass(frozen=True, slots=True)
class CompositionArtifacts:
    root: ProviderCompositionRoot
    deployments: tuple[BaseAIProviderDeployment, ...]
    base_ai_policy: PluginHostPolicy
    runtime_plugin_policy: PluginHostPolicy


def build_provider_composition(
    config: DeploymentCompositionConfig,
    environ: dict[str, str] | MappingProxyType[str, str],
) -> CompositionArtifacts:
    """Preflight secrets and construct the explicitly allowlisted Runtime bridge."""

    if config.runtime_plugin_policy is None:
        raise ValueError("Runtime plugin policy was not loaded")
    secret_resolver = EnvironmentSecretResolver(config.secrets.allowed_env, environ)
    required_secret_names = set(config.user_mcp_key_query.secret_names)
    for provider in config.base_ai_providers:
        required_secret_names.update(provider.secret_names)
    secret_resolver.require_available(tuple(sorted(required_secret_names)))
    signing_env = config.secrets.allowed_env[config.user_mcp_key_query.signing_secret_name]
    deployments = tuple(
        BaseAIProviderDeployment(
            provider_id=item.provider_id,
            package_name=item.package_name,
            package_version=item.package_version,
            package_digest=item.package_digest,
            entry_point=item.entry_point_value,
            entry_point_group=item.entry_point_group,
            api_version=item.api_version,
            enabled_operations=item.enabled_operations,
            egress_hosts=item.egress_hosts,
            secret_names=item.secret_names,
            config=item.config,
        )
        for item in config.base_ai_providers
    )
    base_ai_policy = PluginHostPolicy(
        plugins=tuple(
            AllowedPlugin(
                plugin_id=deployment.plugin_id,
                version=deployment.package_version,
                package_name=deployment.package_name,
                entry_point=deployment.entry_point,
                package_digest=deployment.package_digest,
                permissions=frozenset(deployment.enabled_operations),
                network_targets=frozenset(deployment.egress_hosts),
                secret_names=frozenset(deployment.secret_names),
                config=dict(deployment.config),
            )
            for deployment in deployments
        )
    )

    def build_root(client: httpx.AsyncClient) -> BaseAICompositionRoot:
        query = config.user_mcp_key_query

        def secret(name: str | None) -> str | None:
            return environ[config.secrets.allowed_env[name]] if name is not None else None

        naming = create_nacos_client(
            query.server_addresses,
            namespace=query.namespace,
            username=secret(query.username_secret_name),
            password=secret(query.password_secret_name),
            timeout_seconds=query.timeout_seconds,
        )
        resolver = McpUserKeyResolver(
            discovery=NacosServiceResolver(
                naming, allowed_hosts=query.allowed_hosts, timeout_seconds=query.timeout_seconds
            ),
            client=client,
            codec=UserMcpRefCodec(environ[signing_env].encode("utf-8")),
            group_name=query.group_name,
            timeout_seconds=query.timeout_seconds,
        )
        return BaseAICompositionRoot(
            deployments=deployments,
            plugin_policy=base_ai_policy,
            bootstrap_context=ProviderBootstrapContext(
                secret_resolver=secret_resolver,
                request_credential_resolver=None,
                bound_request_credential_resolver=resolver,
            ),
            supported_runtime_api_version=config.runtime.supported_api_version,
        )

    root = UserKeyCompositionRoot(build_root)
    return CompositionArtifacts(
        root=root,
        deployments=deployments,
        base_ai_policy=base_ai_policy,
        runtime_plugin_policy=config.runtime_plugin_policy,
    )


__all__ = ["CompositionArtifacts", "build_provider_composition"]
