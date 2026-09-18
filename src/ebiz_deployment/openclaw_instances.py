"""OpenClaw host instance material and the §12.2 isolation rule.

Two business agents are two OpenClaw host OS processes: the Adapter plugin is a
module-level singleton keyed by ``deploymentPath`` and refuses a second path in
one process. Everything one process owns must therefore differ between the
Supply Chain instance and the CRM instance; only the sealed host build and the
one Runtime/BFF identity may be shared. This module loads the two files each
instance is described by, checks that rule statically, and renders the
Adapter's protected ``deployment.json`` (reception inlined, trust pins filled)
from the reviewed template.

* ``host.json`` (deployment-owned, ``openclaw-host-instance.v1``): the host
  process facts - port, ``OPENCLAW_STATE_DIR`` / ``OPENCLAW_CONFIG_PATH`` values,
  the env var that backs ``OPENCLAW_GATEWAY_TOKEN``, the installed plugin
  package directory, the ``deploymentPath`` the plugin config points at, the
  reception file and the ingress URL the BFF profile dials.
* ``deployment.json`` (the Adapter's own shape, see its README): connector,
  endpoints, credential env names, generation trust pins, ledger, workspace,
  embedded-agent settings and the inline reception.

Nothing here touches secrets: env var *names* are validated, values never read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HOST_INSTANCE_VERSION = "openclaw-host-instance.v1"
INGRESS_PATH = "/ebiz/tool-gateway/ingress"
_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SECRET_ENV = re.compile(r"^EBIZ_[A-Z0-9_]+$")
_GATEWAY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _pure(value: str) -> PurePath:
    """Interpret the path by its own spelling, not by the validating platform."""

    return PureWindowsPath(value) if _WINDOWS_PATH.match(value) else PurePosixPath(value)


def _gateway_id(value: str) -> str:
    if _GATEWAY_ID.fullmatch(value) is None:
        raise ValueError("gateway identifier required")
    return value


def _absolute(value: str) -> str:
    if not value.strip() or not _pure(value).is_absolute():
        raise ValueError("absolute path required")
    return value


def _loopback_or_https(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not (
            parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname == "127.0.0.1")
        )
    ):
        raise ValueError("endpoint must be HTTPS or literal loopback HTTP")
    return value


class GenerationTrust(_Strict):
    """The Adapter's ``loadGeneration`` options: the trust pins of one signed artifact."""

    path: str
    deployment_root: str = Field(alias="deploymentRoot")
    public_keys: dict[str, str] = Field(alias="publicKeys", min_length=1)
    expected_payload_digest: str = Field(alias="expectedPayloadDigest")
    tenant_id: str = Field(alias="tenantId", min_length=1, max_length=128)
    audience: str
    generation_id: str = Field(alias="generationId")

    @field_validator("path", "deployment_root")
    @classmethod
    def _paths(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("audience", "generation_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _gateway_id(value)

    @field_validator("expected_payload_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("expectedPayloadDigest must be a lowercase SHA-256")
        return value

    @field_validator("public_keys")
    @classmethod
    def _keys(cls, value: dict[str, str]) -> dict[str, str]:
        for key_id, pem in value.items():
            if _KEY_ID.fullmatch(key_id) is None or "BEGIN PUBLIC KEY" not in pem:
                raise ValueError("publicKeys map key ids to SPKI PEM public keys")
        return value

    @model_validator(mode="after")
    def _inside_root(self) -> GenerationTrust:
        root = _pure(self.deployment_root)
        path = _pure(self.path)
        if not path.is_relative_to(root) or path == root:
            raise ValueError("generation path must lie under deploymentRoot")
        return self


class AdapterDeployment(_Strict):
    """The protected deployment JSON one Adapter instance loads (its README's fields)."""

    connector: str = Field(min_length=1, max_length=64)
    gateway_origin: str = Field(alias="gatewayOrigin")
    credential_url: str = Field(alias="credentialUrl")
    end_url: str = Field(alias="endUrl")
    connector_credential_env: str = Field(alias="connectorCredentialEnv")
    ingress_credential_env: str = Field(alias="ingressCredentialEnv")
    generation: GenerationTrust
    retained_generations: tuple[GenerationTrust, ...] = Field(
        alias="retainedGenerations", max_length=32
    )
    ledger_path: str = Field(alias="ledgerPath")
    workspace_dir: str = Field(alias="workspaceDir")
    agent_id: str = Field(alias="agentId")
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    timeout_ms: int = Field(alias="timeoutMs", ge=1000, le=300000)
    reception: dict[str, Any] | None = None

    @field_validator("gateway_origin", "credential_url", "end_url")
    @classmethod
    def _endpoints(cls, value: str) -> str:
        return _loopback_or_https(value)

    @field_validator("ledger_path", "workspace_dir")
    @classmethod
    def _paths(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("connector_credential_env", "ingress_credential_env")
    @classmethod
    def _env(cls, value: str) -> str:
        if _SECRET_ENV.fullmatch(value) is None:
            raise ValueError("credential env names must match ^EBIZ_[A-Z0-9_]+$")
        return value

    @field_validator("agent_id")
    @classmethod
    def _agent(cls, value: str) -> str:
        if _AGENT_ID.fullmatch(value) is None:
            raise ValueError("agentId must be a normalized identifier")
        return value

    @model_validator(mode="after")
    def _distinct_secret_sources(self) -> AdapterDeployment:
        if self.connector_credential_env == self.ingress_credential_env:
            raise ValueError("connector and ingress credentials need different env names")
        return self


class HostInstance(_Strict):
    """The host-process facts of one OpenClaw instance (deployment-owned)."""

    version: str
    agent_id: str = Field(alias="agentId")
    host_root: str = Field(alias="hostRoot")
    port: int = Field(ge=1024, le=65535)
    state_dir: str = Field(alias="stateDir")
    config_path: str = Field(alias="configPath")
    gateway_token_env: str = Field(alias="gatewayTokenEnv")
    plugin_install_dir: str = Field(alias="pluginInstallDir")
    deployment_path: str = Field(alias="deploymentPath")
    reception_path: str = Field(alias="receptionPath", min_length=1)
    ingress_url: str = Field(alias="ingressUrl")

    @field_validator(
        "host_root", "state_dir", "config_path", "plugin_install_dir", "deployment_path"
    )
    @classmethod
    def _paths(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != HOST_INSTANCE_VERSION:
            raise ValueError(f"host instance version must be {HOST_INSTANCE_VERSION}")
        return value

    @field_validator("agent_id")
    @classmethod
    def _agent(cls, value: str) -> str:
        if _AGENT_ID.fullmatch(value) is None:
            raise ValueError("agentId must be a normalized identifier")
        return value

    @field_validator("gateway_token_env")
    @classmethod
    def _env(cls, value: str) -> str:
        if _SECRET_ENV.fullmatch(value) is None:
            raise ValueError("gatewayTokenEnv must match ^EBIZ_[A-Z0-9_]+$")
        return value

    @model_validator(mode="after")
    def _ingress_is_this_host(self) -> HostInstance:
        parsed = urlsplit(self.ingress_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != self.port
            or parsed.path != INGRESS_PATH
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ingressUrl must be this host's loopback port plus " + INGRESS_PATH)
        return self


class InstanceMaterial(_Strict):
    """One instance: its host facts and the Adapter deployment it will load."""

    host: HostInstance
    adapter: AdapterDeployment

    @model_validator(mode="after")
    def _consistent(self) -> InstanceMaterial:
        if self.host.agent_id != self.adapter.agent_id:
            raise ValueError("host and adapter agentId differ")
        return self

    @property
    def agent_id(self) -> str:
        return self.host.agent_id


#: Field paths that must differ between any two instances (design §12.2) and
#: the reason each is process-owned.
DISTINCT_FIELDS: tuple[tuple[str, str], ...] = (
    ("host.agent_id", "one agent id per BFF profile and host"),
    ("host.port", "one listening port per host process"),
    ("host.state_dir", "OPENCLAW_STATE_DIR is process state"),
    ("host.config_path", "OPENCLAW_CONFIG_PATH names one host config"),
    ("host.gateway_token_env", "OPENCLAW_GATEWAY_TOKEN is one host's secret"),
    ("host.plugin_install_dir", "openclaw.plugin.json is materialized per generation"),
    ("host.deployment_path", "the plugin singleton is keyed by deploymentPath"),
    ("host.reception_path", "one reception per agent"),
    ("host.ingress_url", "the BFF profile dials its own instance"),
    ("adapter.connector", "the ledger keys rows by (connector, occurrence)"),
    ("adapter.credential_url", "each BFF profile issues its own run credentials"),
    ("adapter.end_url", "each BFF profile ends its own runs"),
    ("adapter.connector_credential_env", "one connector bearer per instance"),
    ("adapter.ingress_credential_env", "one ingress bearer per instance"),
    ("adapter.generation.path", "one signed generation per instance"),
    ("adapter.generation.generation_id", "one generation id per instance"),
    ("adapter.ledger_path", "SQLite holds a process-lifetime ownership lock"),
    ("adapter.workspace_dir", "one embedded-agent workspace per instance"),
)

#: Field paths that must be equal between instances: the sealed host and the
#: one Runtime / BFF identity domain.
SHARED_FIELDS: tuple[tuple[str, str], ...] = (
    ("host.host_root", "one sealed host build and patch"),
    ("adapter.gateway_origin", "one Runtime Tool Gateway"),
    ("adapter.generation.tenant_id", "one tenant per deployment (D-2)"),
    ("adapter.generation.audience", "one deployment audience"),
)


def _lookup(material: InstanceMaterial, dotted: str) -> Any:
    value: Any = material
    for part in dotted.split("."):
        value = getattr(value, part)
    return value


def _comparable(value: Any) -> Any:
    # Paths and URLs on this deployment are compared case-insensitively and
    # separator-insensitively so ``C:\a\B`` and ``c:/a/b`` count as one.
    if isinstance(value, str):
        return value.replace("\\", "/").rstrip("/").casefold()
    return value


def assert_instances_isolated(instances: Iterable[InstanceMaterial]) -> None:
    """Raise ``ValueError`` naming the first §12.2 rule two instances break."""

    items = tuple(instances)
    if len(items) < 2:
        raise ValueError("instance isolation is a property of at least two instances")
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            pair = f"{left.agent_id}/{right.agent_id}"
            for field, reason in DISTINCT_FIELDS:
                if _comparable(_lookup(left, field)) == _comparable(_lookup(right, field)):
                    raise ValueError(f"instances {pair} share {field} ({reason})")
            for field, reason in SHARED_FIELDS:
                if _comparable(_lookup(left, field)) != _comparable(_lookup(right, field)):
                    raise ValueError(f"instances {pair} differ in {field} ({reason})")


def load_instance_material(
    directory: Path,
    *,
    host_name: str = "host.json",
    deployment_name: str = "deployment.json",
) -> InstanceMaterial:
    host = json.loads((directory / host_name).read_text(encoding="utf-8"))
    adapter = json.loads((directory / deployment_name).read_text(encoding="utf-8"))
    return InstanceMaterial(
        host=HostInstance.model_validate(host),
        adapter=AdapterDeployment.model_validate(adapter),
    )


def render_adapter_deployment(
    material: InstanceMaterial,
    *,
    repo_root: Path,
    generation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The Adapter's ``deployment.json`` document: template plus inline reception.

    ``generation`` replaces the template's trust pins with the ones of the
    artifact actually signed for this instance (path, deploymentRoot, publicKeys,
    expectedPayloadDigest, tenantId, audience, generationId). The reception file
    named by the host manifest is inlined verbatim; its ``tools`` keys must name
    offers of the instance's generation, which the Adapter enforces at load.
    """

    document: dict[str, Any] = json.loads(
        material.adapter.model_dump_json(by_alias=True, exclude_none=True)
    )
    reception = json.loads((repo_root / material.host.reception_path).read_text(encoding="utf-8"))
    document["reception"] = reception
    if generation is not None:
        document["generation"] = json.loads(
            GenerationTrust.model_validate(dict(generation)).model_dump_json(by_alias=True)
        )
    document["retainedGenerations"] = [
        json.loads(item.model_dump_json(by_alias=True))
        for item in material.adapter.retained_generations
    ]
    return document


def write_json_once(path: Path, document: Mapping[str, Any]) -> None:
    """Write protected instance material once; never overwrite an existing file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


__all__ = [
    "DISTINCT_FIELDS",
    "HOST_INSTANCE_VERSION",
    "INGRESS_PATH",
    "SHARED_FIELDS",
    "AdapterDeployment",
    "GenerationTrust",
    "HostInstance",
    "InstanceMaterial",
    "assert_instances_isolated",
    "load_instance_material",
    "render_adapter_deployment",
    "write_json_once",
]
