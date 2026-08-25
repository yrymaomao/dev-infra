"""Construct the deployment-owned Base AI composition root."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from agent_runtime.base_ai.composition import BaseAICompositionRoot, BaseAIProviderDeployment
from agent_runtime.plugins.manifest import AllowedPlugin, PluginHostPolicy
from base_ai.providers import ProviderBootstrapContext

from .config import DeploymentCompositionConfig
from .credentials import EnvironmentSecretResolver, HttpsCredentialBrokerResolver


@dataclass(frozen=True, slots=True)
class CompositionArtifacts:
    root: BaseAICompositionRoot
    deployments: tuple[BaseAIProviderDeployment, ...]
    base_ai_policy: PluginHostPolicy
    runtime_plugin_policy: PluginHostPolicy


def build_provider_composition(
    config: DeploymentCompositionConfig,
    environ: dict[str, str] | MappingProxyType[str, str],
) -> CompositionArtifacts:
    """Preflight secrets and construct the exact three-provider Runtime bridge."""

    if config.runtime_plugin_policy is None:
        raise ValueError("Runtime plugin policy was not loaded")
    secret_resolver = EnvironmentSecretResolver(config.secrets.allowed_env, environ)
    required_secret_names = {config.credential_broker.auth_secret_name}
    for provider in config.base_ai_providers:
        required_secret_names.update(provider.secret_names)
    secret_resolver.require_available(tuple(sorted(required_secret_names)))
    credential_resolver = HttpsCredentialBrokerResolver(
        url=config.credential_broker.url,
        auth_secret_name=config.credential_broker.auth_secret_name,
        allowed_provider_ids=config.credential_broker.allowed_provider_ids,
        timeout_seconds=config.credential_broker.timeout_seconds,
        secret_resolver=secret_resolver,
    )
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
    root = BaseAICompositionRoot(
        deployments=deployments,
        plugin_policy=base_ai_policy,
        bootstrap_context=ProviderBootstrapContext(
            secret_resolver=secret_resolver,
            request_credential_resolver=credential_resolver,
        ),
        supported_runtime_api_version=config.runtime.supported_api_version,
    )
    return CompositionArtifacts(
        root=root,
        deployments=deployments,
        base_ai_policy=base_ai_policy,
        runtime_plugin_policy=config.runtime_plugin_policy,
    )


__all__ = ["CompositionArtifacts", "build_provider_composition"]
