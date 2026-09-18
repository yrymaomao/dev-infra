"""R1-11 (export half): one signed-generation payload per OpenClaw instance.

The CRM payload names exactly ``crm_case_advice``; the Supply Chain payload is
byte-identical to today's single export; the Runtime composition answers each
instance from its own generation.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_runtime.application.tool_gateway_catalog import ToolGatewayCatalogService
from agent_runtime.application.tool_gateway_export import export_gateway_generation
from ebiz_runtime_contracts.tool_gateway_catalog import (
    CatalogSearchRequest,
    CatalogVisibilityRequest,
    InvocationRequest,
    ToolOfferReference,
)
from gateway_export_fixtures import FixtureProjector
from test_crm_gateway_composition import configure_crm

from ebiz_deployment import generation_export as job
from ebiz_deployment.config import load_deployment_config
from ebiz_deployment.launcher import build_gateway_composition
from ebiz_deployment.tool_gateway import (
    ROUTED_CATALOG_METHODS,
    InstanceRoutedCatalog,
    SupplyChainToolGatewayComposition,
)

AUDIENCE = "ebizhub-openclaw"


def two_instance_composition(tmp_path: Path, **extra_env: str):
    path, _, env = configure_crm(tmp_path)
    env.update(extra_env)
    composition = build_gateway_composition(env, load_deployment_config(path, env))
    assert composition is not None
    return composition, env


def supply_chain_only(env: dict[str, str]) -> SupplyChainToolGatewayComposition:
    """Today's single-offer composition, exactly as a Supply Chain deployment builds it."""

    return SupplyChainToolGatewayComposition(
        workflow_digest=env["SUPPLY_CHAIN_ON_DEMAND_WORKFLOW_DIGEST"],
        cid="supply-chain-dev",
        credential_ref=env["SUPPLY_CHAIN_CREDENTIAL_REF"],
        tenant_id=env["BFF_OPENCLAW_TENANT_ID"],
        bff_url=env["SUPPLY_CHAIN_BFF_INTERNAL_URL"],
        connector_credential=env["BFF_OPENCLAW_CONNECTOR_CREDENTIAL"],
        jwt_key=env["TOOL_GATEWAY_JWT_KEY"],
        jwt_issuer="ebizhub-supply-chain-bff",
        jwt_audience="ebizhub-tool-gateway",
        generation_id=env["TOOL_GATEWAY_GENERATION_ID"],
        catalog_revision=env["TOOL_GATEWAY_CATALOG_REVISION"],
    )


async def test_each_instance_exports_only_its_own_offer_under_its_own_generation(tmp_path):
    composition, env = two_instance_composition(tmp_path)
    projector = FixtureProjector()
    exports = await job.export_instance_generations(
        composition, projector=projector, deployment_audience=AUDIENCE
    )
    by_agent = {export.agent_id: export for export in exports}
    assert list(by_agent) == ["main", "crm"]
    crm, supply_chain = by_agent["crm"], by_agent["main"]

    crm_document = json.loads(crm.payload)
    assert {offer["name"] for offer in crm_document["offers"]} == {"crm_case_advice"}
    assert crm.tool_names == ("crm_case_advice",)
    assert crm_document["offers"][0]["aliases"] == [
        "crm_case_analysis",
        "crm_case_advise_on_demand",
    ]
    assert list(crm_document["offers"][0]["input_schema"]["properties"]) == ["case_id"]
    assert b"inventory_supply_chain_on_demand" not in crm.payload
    assert b"skus" not in crm.payload

    # Distinct generation ids; same tenant, audience and catalog revision source.
    assert (supply_chain.generation_id, crm.generation_id) == ("sc-crm-1", "sc-crm-1-crm")
    supply_document = json.loads(supply_chain.payload)
    for document in (supply_document, crm_document):
        assert document["tenant_id"] == "tenant-a"
        assert document["deployment_audience"] == AUDIENCE
        assert document["catalog_revision"] == "sc-crm-1"
        assert all(
            offer["reference"]["generation_id"] == document["generation_id"]
            for offer in document["offers"]
        )
    assert crm.digest == hashlib.sha256(crm.payload).hexdigest()
    assert crm.filename == "generation-crm.json"

    # The Supply Chain instance keeps today's generation: byte for byte the single export.
    single = await export_gateway_generation(
        generation=supply_chain_only(env).generation,
        projector=projector,  # type: ignore[arg-type]
        deployment_audience=AUDIENCE,
    )
    assert supply_chain.payload == single
    assert {offer["name"] for offer in supply_document["offers"]} == {
        "inventory_supply_chain_on_demand"
    }


async def test_single_export_path_is_unchanged_and_refuses_a_multi_instance_composition(
    tmp_path,
):
    composition, env = two_instance_composition(tmp_path)
    projector = FixtureProjector()
    single = supply_chain_only(env)
    assert [instance.agent_id for instance in single.instances] == ["main"]
    assert single.generation.generation_id == "sc-crm-1"
    payload = await job.export_generation(single, projector=projector, deployment_audience=AUDIENCE)
    assert payload == await export_gateway_generation(
        generation=single.generation,
        projector=projector,  # type: ignore[arg-type]
        deployment_audience=AUDIENCE,
    )
    with pytest.raises(ValueError, match="several OpenClaw instances"):
        _ = composition.generation
    with pytest.raises(ValueError, match="several OpenClaw instances"):
        await job.export_generation(composition, projector=projector, deployment_audience=AUDIENCE)


def test_crm_generation_id_is_explicit_or_derived_and_never_equal(tmp_path):
    explicit, _ = two_instance_composition(
        tmp_path, CRM_TOOL_GATEWAY_GENERATION_ID="crm-generation-7"
    )
    assert [g.generation_id for g in explicit.generations] == ["sc-crm-1", "crm-generation-7"]
    assert [i.tool_names for i in explicit.instances] == [
        ("inventory_supply_chain_on_demand",),
        ("crm_case_advice",),
    ]
    (tmp_path / "same").mkdir()
    with pytest.raises(ValueError, match="its own generation id"):
        two_instance_composition(tmp_path / "same", CRM_TOOL_GATEWAY_GENERATION_ID="sc-crm-1")


async def test_export_refuses_a_projector_that_answers_with_foreign_offers(tmp_path):
    composition, _ = two_instance_composition(tmp_path)
    honest = FixtureProjector()

    class Renaming:
        async def project_exact(self, generation, pin):
            projection = await honest.project_exact(generation, pin)
            if pin.name == "crm_case_advice":
                # A wrong catalog answer: the CRM instance would load a foreign tool.
                return SimpleNamespace(
                    offer=projection.offer.model_copy(
                        update={"name": "inventory_supply_chain_on_demand"}
                    )
                )
            return projection

    with pytest.raises(job.GenerationExportError, match="'crm' does not match its pins"):
        await job.export_instance_generations(
            composition, projector=Renaming(), deployment_audience=AUDIENCE
        )


def test_routed_catalog_forwards_every_public_read_by_generation_id():
    public = {
        name
        for name, member in inspect.getmembers(ToolGatewayCatalogService)
        if not name.startswith("_") and inspect.iscoroutinefunction(member) and name != "create"
    }
    assert public == set(ROUTED_CATALOG_METHODS)
    first = SimpleNamespace(**{name: AsyncMock(return_value=name + "-1") for name in public})
    second = SimpleNamespace(**{name: AsyncMock(return_value=name + "-2") for name in public})
    catalog = InstanceRoutedCatalog({"gen-1": first, "gen-2": second}, default="gen-1")  # type: ignore[dict-item]
    assert catalog.generation_ids == ("gen-1", "gen-2")

    async def run():
        visibility = CatalogVisibilityRequest(
            generation_id="gen-2", candidates=("crm-case-advice",)
        )
        assert await catalog.visibility("Bearer x", visibility) == "visibility-2"
        search = CatalogSearchRequest(generation_id="gen-1", query="crm")
        assert await catalog.search(None, search) == "search-1"
        reference = ToolOfferReference(
            generation_id="gen-2",
            offer_id="crm-case-advice",
            version=1,
            publication_digest="b" * 64,
            input_schema_digest="c" * 64,
            output_schema_digest="d" * 64,
        )
        assert await catalog.describe(None, reference=reference) == "describe-2"
        invocation = InvocationRequest(reference=reference, request_key="k1", arguments={})
        assert await catalog.prepare_invocation(None, invocation) == "prepare_invocation-2"
        # An unknown generation reaches the default service, which authorizes first
        # and then raises Runtime's own publication conflict - no id enumeration.
        unknown = CatalogVisibilityRequest(generation_id="gen-9", candidates=())
        assert await catalog.visibility(None, unknown) == "visibility-1"

    import asyncio

    asyncio.run(run())
    assert second.visibility.await_args.args[0] == "Bearer x"
    with pytest.raises(ValueError):
        InstanceRoutedCatalog({"gen-1": first}, default="gen-2")  # type: ignore[dict-item]


async def test_cli_writes_each_payload_once_and_prints_the_pins(tmp_path, capsys):
    composition, _ = two_instance_composition(tmp_path)
    exports = await job.export_instance_generations(
        composition, projector=FixtureProjector(), deployment_audience=AUDIENCE
    )
    out_dir = tmp_path / "out"
    argv = ["--out-dir", str(out_dir), "--audience", AUDIENCE, "--format", "json"]
    assert job.main(argv, run=lambda arguments, url: exports) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "PASS"
    assert [item["agent_id"] for item in receipt["instances"]] == ["main", "crm"]
    for item in receipt["instances"]:
        written = Path(item["path"]).read_bytes()
        assert Path(item["path"]).name == f"generation-{item['agent_id']}.json"
        assert hashlib.sha256(written).hexdigest() == item["expected_payload_digest"]
        assert item["bytes"] == len(written)
    crm = receipt["instances"][1]
    assert crm["tool_names"] == ["crm_case_advice"]
    assert crm["generation_id"] == "sc-crm-1-crm"
    # Never overwritten.
    assert job.main(argv, run=lambda arguments, url: exports) == 1
    assert "never overwritten" in capsys.readouterr().err
    assert job.main(["--out-dir", str(out_dir), "--audience", "bad audience!"], run=None) == 2
    assert job.main(["--out-dir", str(out_dir), "--audience", AUDIENCE], environ={}) == 1
    assert "database is unavailable" in capsys.readouterr().err

    def failing(arguments, url):
        raise RuntimeError("postgres://secret@host/db")

    assert job.main(argv, run=failing) == 1
    assert "secret" not in capsys.readouterr().err
