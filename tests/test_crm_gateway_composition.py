from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_runtime.application.tool_gateway_operations import GatewayOperationConflict
from ebiz_runtime_contracts.tool_gateway_catalog import ToolOfferReference
from ebiz_runtime_contracts.tool_gateway_policy import PolicyTarget, ToolGatewayPolicyReply
from test_composition_launcher import configured_files

from ebiz_deployment.config import CRM_READ_TOOL_BY_OPERATION, load_deployment_config
from ebiz_deployment.launcher import CRM_SCOPES, SUPPLY_CHAIN_SCOPES, launch
from ebiz_deployment.tool_gateway import DynamicGatewayContext, SharedToolGatewayComposition

CRM_DIGEST = "b" * 64


def crm_plugin(plugin_id="ebizhub.crm-agent", entry="crm_agent.plugin:factory"):
    return {
        "plugin_id": plugin_id,
        "version": "2.0.0",
        "package_name": "ebiz-agent-crm",
        "entry_point": entry,
        "package_digest": "e" * 64,
        "permissions": ["crm.compute", "crm.preview"],
        "network_targets": [],
        "secret_names": [],
        "config": {"policy_version": "crm-policy-v1"},
    }


def crm_release():
    pin = {
        "distribution_name": "ebiz-agent-crm",
        "distribution_version": "2.0.0",
        "record_digest": "e" * 64,
    }
    return {
        "agent_id": "crm",
        "agent_distribution": "ebiz-agent-crm",
        "agent_distribution_version": "2.0.0",
        "agent_record_digest": "e" * 64,
        "advise_workflow": {
            "code": "crm-case-advise-on-demand",
            "version": 1,
            "digest": CRM_DIGEST,
        },
        "capability_sets": [
            {"set_id": "crm", "version": 2, **pin},
            {"set_id": "crm-advise", "version": 1, **pin},
        ],
    }


def configure_crm(tmp_path):
    path, policy_path, env = configured_files(tmp_path)
    document = json.loads(path.read_text())
    document["credential_broker"]["allowed_provider_ids"].append("yeaher.crm")
    mcp = document["base_ai_providers"][0]
    mcp["config"]["allowed_tools"] = sorted(
        mcp["config"]["allowed_tools"] + list(CRM_READ_TOOL_BY_OPERATION.values())
    )
    document["base_ai_providers"].append(
        {
            "provider_id": "yeaher.crm",
            "package_name": "ebiz-adapter-crm",
            "package_version": "0.1.0",
            "record_digest": "c" * 64,
            "entry_point_group": "base_ai.provider_factories",
            "entry_point_value": "ebiz_adapter_crm:CrmProviderFactory",
            "api_version": "v1",
            "enabled_operations": list(CRM_READ_TOOL_BY_OPERATION),
            "egress_hosts": [],
            "secret_names": [],
            "config": {"mcp": {"tools": CRM_READ_TOOL_BY_OPERATION}},
        }
    )
    document["crm_release"] = crm_release()
    path.write_text(json.dumps(document))
    policy = json.loads(policy_path.read_text())
    policy["plugins"].append(crm_plugin())
    policy_path.write_text(json.dumps(policy))
    env.update(
        {
            "SUPPLY_CHAIN_TOOL_GATEWAY_ENABLED": "true",
            "CRM_TOOL_GATEWAY_ENABLED": "true",
            "SUPPLY_CHAIN_ON_DEMAND_WORKFLOW_DIGEST": "a" * 64,
            "CRM_ADVISOR_WORKFLOW_DIGEST": CRM_DIGEST,
            "SUPPLY_CHAIN_CREDENTIAL_REF": "opaque:supply-chain",
            "CRM_CREDENTIAL_REF": "opaque:crm-readonly",
            "BFF_OPENCLAW_CONNECTOR_CREDENTIAL": "c" * 32,
            "TOOL_GATEWAY_JWT_KEY": "j" * 32,
            "SUPPLY_CHAIN_BFF_INTERNAL_URL": "http://127.0.0.1:8089",
            "BFF_OPENCLAW_TENANT_ID": "tenant-a",
            "TOOL_GATEWAY_GENERATION_ID": "sc-crm-1",
            "TOOL_GATEWAY_CATALOG_REVISION": "sc-crm-1",
        }
    )
    return path, policy_path, env


def reference(pin):
    return ToolOfferReference(
        generation_id="sc-crm-1",
        offer_id=pin.offer_id,
        version=pin.version,
        publication_digest=pin.publication_digest,
        input_schema_digest="a" * 64,
        output_schema_digest="b" * 64,
    )


def test_launcher_injects_one_composition_with_two_pins_and_isolated_credentials(
    tmp_path, monkeypatch
):
    _, _, env = configure_crm(tmp_path)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    received = {}

    def runtime_main(argv, **kwargs):
        received.update(kwargs)
        return 0

    assert launch([], environ=env, runtime_main=runtime_main) == 0
    shared = received["gateway_composition"]
    assert isinstance(shared, SharedToolGatewayComposition)
    assert [p.code for p in shared.generation.pins] == [
        "inventory-supply-chain-on-demand",
        "crm-case-advise-on-demand",
    ]
    profiles = tuple((reference(offer.pin), offer.authority) for offer in shared.offers)
    resolver = DynamicGatewayContext(
        connector_id="openclaw", tenant_id="tenant-a", profiles=profiles
    )
    for ref, authority in profiles:
        decision = ToolGatewayPolicyReply(
            tenant_id="tenant-a",
            principal_id="operator",
            session_key=authority.session_key_prefix + "0" * 48,
            binding_active=True,
            action="invoke",
            policy_revision="1",
            allowed_targets=(PolicyTarget(kind="offer", target_id=ref.offer_id),),
        )
        resolved = resolver.resolve(decision, ref)
        assert resolved.auth.scopes == authority.scopes
        assert resolved.auth.credential_ref == authority.credential_ref
        for changed in [
            {"generation_id": "old"},
            {"publication_digest": "f" * 64},
            {"output_schema_digest": "f" * 64},
        ]:
            with pytest.raises(GatewayOperationConflict):
                resolver.resolve(decision, ref.model_copy(update=changed))
        other_prefix = next(a.session_key_prefix for _, a in profiles if a is not authority)
        for changed in [
            {"tenant_id": "tenant-b"},
            {"binding_active": False},
            {"allowed_targets": ()},
            {"action": "catalog"},
            # a run bound by the other profile's BFF never borrows these scopes
            {"session_key": other_prefix + "0" * 48},
        ]:
            with pytest.raises((GatewayOperationConflict, ValueError)):
                resolver.resolve(decision.model_copy(update=changed), ref)
    # R1-05: the CRM offer carries exactly the eight advise scopes, the Supply Chain
    # offer exactly its five; neither borrows from the other.
    assert shared.offers[0].authority.scopes == SUPPLY_CHAIN_SCOPES
    assert shared.offers[1].authority.scopes == CRM_SCOPES
    assert len(CRM_SCOPES) == 8 and "inventory.read" not in CRM_SCOPES
    assert shared.offers[1].pin.name == "crm_case_advice"
    assert shared.offers[1].pin.aliases == ("crm_case_analysis", "crm_case_advise_on_demand")
    assert shared.offers[1].pin.version == 1
    assert shared.offers[1].pin.publication_digest == CRM_DIGEST
    assert [r.authorize_path for r in shared.routes] == [
        "/internal/crm/v2/openclaw/authorize",
        "/internal/supply-chain/v2/openclaw/authorize",
    ]
    assert shared.offers[1].authority.session_key_prefix == "agent:crm:openclaw:"
    assert shared.offers[0].authority.session_key_prefix == "agent:main:openclaw:"
    assert not any(s.startswith("crm.") for s in shared.offers[0].authority.scopes)
    assert not any(
        s.startswith("inventory.") or "write" in s or "send" in s
        for s in shared.offers[1].authority.scopes
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "write_operation",
        "write_tool",
        "broker",
        "plugin",
        "second_plugin",
        "release_missing",
        "release_sets",
    ],
)
def test_crm_deployment_fails_closed_on_write_or_mismatched_authority(tmp_path, mutation):
    path, policy_path, env = configure_crm(tmp_path)
    document = json.loads(path.read_text())
    policy = json.loads(policy_path.read_text())
    if mutation == "write_operation":
        document["base_ai_providers"][-1]["enabled_operations"].append("crm.send_case_message")
    elif mutation == "write_tool":
        document["base_ai_providers"][0]["config"]["allowed_tools"].append(
            "crm_send_case_message_v1"
        )
    elif mutation == "broker":
        document["credential_broker"]["allowed_provider_ids"].remove("yeaher.crm")
    elif mutation == "plugin":
        policy["plugins"][-1]["permissions"].append("crm.message.send")
    elif mutation == "second_plugin":
        # There is no ebizhub.crm-advisor plugin in the CRM wheel; a second pin is refused.
        policy["plugins"].append(crm_plugin("ebizhub.crm-advisor", "crm_advisor.plugin:factory"))
    elif mutation == "release_missing":
        del document["crm_release"]
    else:
        document["crm_release"]["capability_sets"].pop()
    path.write_text(json.dumps(document))
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(ValueError):
        load_deployment_config(path, env)


def test_launcher_refuses_a_workflow_digest_that_differs_from_the_release_pin(
    tmp_path, monkeypatch
):
    _, _, env = configure_crm(tmp_path)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    env["CRM_ADVISOR_WORKFLOW_DIGEST"] = "f" * 64
    with pytest.raises(ValueError, match="crm_release.advise_workflow.digest"):
        launch([], environ=env, runtime_main=lambda argv, **kwargs: 0)


@pytest.mark.asyncio
async def test_composition_builds_preparation_for_each_pin_but_only_one_lifecycle(
    tmp_path, monkeypatch
):
    from ebiz_deployment import tool_gateway as gateway

    _, _, env = configure_crm(tmp_path)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    captured = {}

    def runtime_main(argv, **kwargs):
        captured.update(kwargs)
        return 0

    launch([], environ=env, runtime_main=runtime_main)
    shared = captured["gateway_composition"]
    publications = {
        offer.pin.code: SimpleNamespace(checksum=offer.pin.publication_digest)
        for offer in shared.offers
    }
    workflows = SimpleNamespace(
        get_published=AsyncMock(side_effect=lambda tenant, code, version: publications[code])
    )
    monkeypatch.setattr(gateway, "PostgresWorkflowRegistry", lambda _: workflows)
    monkeypatch.setattr(gateway, "PostgresCapabilityRegistry", lambda _: object())
    projector = SimpleNamespace(
        project_exact=AsyncMock(
            side_effect=lambda gen, pin: SimpleNamespace(
                offer=SimpleNamespace(reference=reference(pin))
            )
        )
    )
    monkeypatch.setattr(gateway, "RegistryToolOfferProjector", lambda *args: projector)
    monkeypatch.setattr(
        gateway.ToolGatewayCatalogService, "create", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(gateway, "ToolGatewaySnapshotService", lambda **kwargs: object())
    monkeypatch.setattr(gateway, "GatewaySnapshotPreparation", lambda **kwargs: kwargs)

    def preparations(**kwargs):
        captured["preparations"] = kwargs["preparations"]
        return object()

    monkeypatch.setattr(gateway, "ConfiguredGatewayPreparations", preparations)
    monkeypatch.setattr(gateway, "GatewayOperationRepository", lambda _: object())
    monkeypatch.setattr(gateway, "GatewayOperationService", lambda **kwargs: object())
    monkeypatch.setattr(gateway, "GatewayOperationBackend", lambda **kwargs: object())
    monkeypatch.setattr(gateway, "GatewayReconciliationTransport", lambda **kwargs: object())
    monkeypatch.setattr(gateway, "async_sessionmaker", lambda *args, **kwargs: object())
    workers = []
    monkeypatch.setattr(gateway, "PostgresOutboxWorker", lambda **kwargs: workers.append(kwargs))
    lifecycle = SimpleNamespace(aclose=AsyncMock())
    clients = []

    def start(**kwargs):
        clients.append(kwargs["client"])
        return lifecycle

    monkeypatch.setattr(gateway._GatewayLifecycle, "start", start)
    monkeypatch.setattr(gateway, "ToolGatewayDeliveryService", lambda **kwargs: object())
    container = SimpleNamespace(
        unit_of_work_factory=object(),
        governed_artifact_service=object(),
        payload_store=object(),
        service=object(),
        engine=object(),
    )
    result = await shared.compose(container, object())
    try:
        assert result.lifecycle is lifecycle
        assert len(workers) == len(clients) == 1
        assert [ref.offer_id for ref, _ in captured["preparations"]] == [
            "supply-chain-on-demand",
            "crm-case-advice",
        ]
        assert [p["pin"] for _, p in captured["preparations"]] == list(shared.generation.pins)
        assert workflows.get_published.await_count == 2
    finally:
        for client in clients:
            await client.aclose()
