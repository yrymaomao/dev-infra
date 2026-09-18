"""R1-11 (instance half): the two committed instance configs differ in every field
design §12.2 requires to differ and share only what it allows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebiz_deployment import openclaw_instances as instances
from ebiz_deployment.openclaw_instances import (
    DISTINCT_FIELDS,
    SHARED_FIELDS,
    InstanceMaterial,
    assert_instances_isolated,
    load_instance_material,
    render_adapter_deployment,
    write_json_once,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "config/openclaw"


def committed() -> tuple[InstanceMaterial, InstanceMaterial]:
    kwargs = {"host_name": "host.example.json", "deployment_name": "deployment.example.json"}
    return (
        load_instance_material(EXAMPLES / "supply-chain", **kwargs),
        load_instance_material(EXAMPLES / "crm", **kwargs),
    )


def documents(material: InstanceMaterial) -> tuple[dict, dict]:
    return (
        json.loads(material.host.model_dump_json(by_alias=True)),
        json.loads(material.adapter.model_dump_json(by_alias=True, exclude_none=True)),
    )


def rebuild(host: dict, adapter: dict) -> InstanceMaterial:
    return InstanceMaterial(
        host=instances.HostInstance.model_validate(host),
        adapter=instances.AdapterDeployment.model_validate(adapter),
    )


def test_committed_instances_are_isolated_per_design_12_2() -> None:
    supply_chain, crm = committed()
    assert_instances_isolated((supply_chain, crm))
    assert (crm.agent_id, crm.adapter.connector) == ("crm", "crm")
    assert crm.adapter.connector_credential_env == "EBIZ_CRM_CONNECTOR"
    assert crm.adapter.ingress_credential_env == "EBIZ_CRM_INGRESS"
    assert (supply_chain.host.port, crm.host.port) == (18789, 18790)
    assert crm.host.gateway_token_env == "EBIZ_CRM_GATEWAY_TOKEN"
    assert crm.adapter.generation.generation_id == "supply-chain-v2-dev-1-crm"
    assert supply_chain.adapter.generation.generation_id == "supply-chain-v2-dev-1"
    assert crm.adapter.credential_url.endswith("/api/crm/v2/openclaw/credentials")
    assert crm.adapter.end_url.endswith("/api/crm/v2/openclaw/runs/end")
    assert crm.adapter.retained_generations == ()
    # The ingress each BFF profile dials is the one its env example names.
    env_example = (ROOT / "config/deployment.env.example").read_text(encoding="utf-8")
    assert f"BFF_OPENCLAW_INGRESS_URL={supply_chain.host.ingress_url}" in env_example
    assert f"BFF_CRM_OPENCLAW_INGRESS_URL={crm.host.ingress_url}" in env_example
    # Every §12.2 item is covered by the rule.
    covered = {field for field, _ in DISTINCT_FIELDS}
    assert {
        "host.deployment_path",
        "adapter.ledger_path",
        "adapter.workspace_dir",
        "host.port",
        "host.state_dir",
        "host.config_path",
        "adapter.connector",
        "adapter.connector_credential_env",
        "adapter.ingress_credential_env",
        "host.gateway_token_env",
        "host.plugin_install_dir",
        "adapter.generation.generation_id",
    } <= covered
    assert {"host.host_root", "adapter.generation.tenant_id"} <= {f for f, _ in SHARED_FIELDS}


def _set(document: dict, dotted: str, value: object) -> None:
    *parents, last = dotted.split(".")
    for part in parents:
        document = document[part]
    document[last] = value


def _get(document: dict, dotted: str) -> object:
    for part in dotted.split("."):
        document = document[part]
    return document


ALIAS = {
    "agent_id": "agentId",
    "state_dir": "stateDir",
    "config_path": "configPath",
    "gateway_token_env": "gatewayTokenEnv",
    "plugin_install_dir": "pluginInstallDir",
    "deployment_path": "deploymentPath",
    "reception_path": "receptionPath",
    "ingress_url": "ingressUrl",
    "credential_url": "credentialUrl",
    "end_url": "endUrl",
    "connector_credential_env": "connectorCredentialEnv",
    "ingress_credential_env": "ingressCredentialEnv",
    "generation_id": "generationId",
    "ledger_path": "ledgerPath",
    "workspace_dir": "workspaceDir",
    "host_root": "hostRoot",
    "gateway_origin": "gatewayOrigin",
    "tenant_id": "tenantId",
}


def alias_path(dotted: str) -> tuple[str, str]:
    section, _, rest = dotted.partition(".")
    return section, ".".join(ALIAS.get(part, part) for part in rest.split("."))


@pytest.mark.parametrize("field", [field for field, _ in DISTINCT_FIELDS])
def test_sharing_any_process_owned_item_is_refused(field: str) -> None:
    supply_chain, crm = committed()
    sc_host, sc_adapter = documents(supply_chain)
    crm_host, crm_adapter = documents(crm)
    section, path = alias_path(field)
    source = crm_host if section == "host" else crm_adapter
    target = sc_host if section == "host" else sc_adapter
    _set(target, path, _get(source, path))
    # Keep each mutated document self-consistent (its own validators would
    # otherwise refuse it before the isolation rule runs).
    if field in {"host.port", "host.ingress_url"}:
        sc_host["port"], sc_host["ingressUrl"] = crm_host["port"], crm_host["ingressUrl"]
    if field == "host.agent_id":
        sc_adapter["agentId"] = crm_adapter["agentId"]
    if field == "adapter.generation.path":
        sc_adapter["generation"]["deploymentRoot"] = crm_adapter["generation"]["deploymentRoot"]
    expected = (
        "host[.](port|ingress_url)"
        if field.startswith("host.port") or field.endswith("ingress_url")
        else field.replace(".", "[.]")
    )
    with pytest.raises(ValueError, match=f"share {expected}"):
        assert_instances_isolated((rebuild(sc_host, sc_adapter), crm))


@pytest.mark.parametrize("field", [field for field, _ in SHARED_FIELDS])
def test_diverging_a_shared_item_is_refused(field: str) -> None:
    supply_chain, crm = committed()
    crm_host, crm_adapter = documents(crm)
    section, path = alias_path(field)
    document = crm_host if section == "host" else crm_adapter
    current = _get(document, path)
    assert isinstance(current, str)
    replacement = {
        "host.host_root": "C:\\openclaw\\other-host",
        "adapter.gateway_origin": "http://127.0.0.1:8002",
        "adapter.generation.tenant_id": "tenant-other",
        "adapter.generation.audience": "other-audience",
    }[field]
    _set(document, path, replacement)
    with pytest.raises(ValueError, match=f"differ in {field.replace('.', '[.]')}"):
        assert_instances_isolated((supply_chain, rebuild(crm_host, crm_adapter)))


def test_host_manifest_and_adapter_document_fail_closed() -> None:
    _, crm = committed()
    host, adapter = documents(crm)
    for mutate, message in [
        (lambda h, a: h.update({"version": "openclaw-host-instance.v2"}), "version"),
        (lambda h, a: h.update({"gatewayTokenEnv": "OPENCLAW_GATEWAY_TOKEN"}), "EBIZ_"),
        (
            lambda h, a: h.update(
                {"ingressUrl": "http://127.0.0.1:18789/ebiz/tool-gateway/ingress"}
            ),
            "loopback port",
        ),
        (lambda h, a: h.update({"stateDir": "state"}), "absolute"),
        (lambda h, a: h.update({"extra": 1}), "extra"),
        (lambda h, a: a.update({"connectorCredentialEnv": "EBIZ_CRM_INGRESS"}), "different env"),
        (lambda h, a: a.update({"gatewayOrigin": "http://runtime.internal:8000"}), "loopback"),
        (lambda h, a: a.update({"agentId": "main"}), "agentId differ"),
        (lambda h, a: a["generation"].update({"expectedPayloadDigest": "X" * 64}), "SHA-256"),
        (lambda h, a: a["generation"].update({"path": "C:\\elsewhere\\g.json"}), "deploymentRoot"),
        (lambda h, a: a["generation"].update({"publicKeys": {"k": "not a pem"}}), "SPKI"),
        (lambda h, a: a.update({"timeoutMs": 500}), "timeoutMs"),
    ]:
        h, a = json.loads(json.dumps(host)), json.loads(json.dumps(adapter))
        mutate(h, a)
        with pytest.raises(ValueError, match=message):
            rebuild(h, a)


def test_render_inlines_each_instance_reception_and_the_signed_trust(tmp_path: Path) -> None:
    supply_chain, crm = committed()
    trust = {
        "path": str(tmp_path / "generations" / "generation-crm.signed.json"),
        "deploymentRoot": str(tmp_path / "generations"),
        "publicKeys": {"local-key": "-----BEGIN PUBLIC KEY-----\nAA==\n-----END PUBLIC KEY-----\n"},
        "expectedPayloadDigest": "a" * 64,
        "tenantId": "tenant-a",
        "audience": "ebizhub-openclaw",
        "generationId": "sc-crm-1-crm",
    }
    rendered = render_adapter_deployment(crm, repo_root=ROOT, generation=trust)
    assert set(rendered["reception"]["tools"]) == {"crm_case_advice"}
    assert rendered["reception"]["argument_admissions"]["crm_case_advice"]["mode"] == (
        "exact_bare_token"
    )
    assert rendered["generation"] == trust
    assert rendered["retainedGenerations"] == []
    assert rendered["agentId"] == "crm" and rendered["connector"] == "crm"
    # Exactly the Adapter's documented keys, nothing deployment-internal.
    assert set(rendered) == {
        "connector",
        "gatewayOrigin",
        "credentialUrl",
        "endUrl",
        "connectorCredentialEnv",
        "ingressCredentialEnv",
        "generation",
        "retainedGenerations",
        "ledgerPath",
        "workspaceDir",
        "agentId",
        "provider",
        "model",
        "timeoutMs",
        "reception",
    }
    supply = render_adapter_deployment(supply_chain, repo_root=ROOT)
    assert set(supply["reception"]["tools"]) == {"inventory_supply_chain_on_demand"}
    target = tmp_path / "crm" / "deployment.json"
    write_json_once(target, rendered)
    assert json.loads(target.read_text(encoding="utf-8")) == rendered
    with pytest.raises(FileExistsError):
        write_json_once(target, rendered)
