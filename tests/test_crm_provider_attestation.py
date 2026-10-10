"""CRM is an explicit extension of the exact installed Provider attestation set."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ebiz_deployment import attestation


def descriptors(*, crm: bool = False) -> tuple[SimpleNamespace, ...]:
    values = [
        (
            "mcp.streamable_http",
            "ebiz-adapter-mcp",
            "ebiz_adapter_mcp:McpProviderFactory",
            "streamable-http/1",
        ),
        ("yeaher.erp", "ebiz-adapter-erp", "ebiz_adapter_erp:ErpProviderFactory", "v1"),
        (
            "openai.responses",
            "ebiz-adapter-model-openai",
            "ebiz_adapter_model_openai:OpenAIProviderFactory",
            "responses/v1",
        ),
    ]
    if crm:
        values.append(
            ("yeaher.crm", "ebiz-adapter-crm", "ebiz_adapter_crm:CrmProviderFactory", "v1")
        )
    return tuple(
        SimpleNamespace(
            name=name,
            distribution_name=package,
            value=value,
            api_version=api,
            distribution_version="0.1.0",
            distribution_digest="a" * 64,
            production_eligible=True,
        )
        for name, package, value, api in values
    )


@pytest.fixture(autouse=True)
def unrelated_agent_attestations(monkeypatch: pytest.MonkeyPatch) -> None:
    # No wheel rebuild or foreign Supply Chain WIP is involved in this unit loop.
    for function in (
        "attest_installed_supply_chain",
        "attest_installed_supply_chain_planning",
        "attest_installed_inventory_catalog",
        "attest_installed_commerce_sales_catalog",
    ):
        monkeypatch.setattr(
            attestation, function, lambda **kwargs: SimpleNamespace(canonical_digest="b" * 64)
        )


def test_default_supply_chain_attestation_stays_exactly_three() -> None:
    output = attestation.build_attestation_environment(descriptors())
    assert "CRM_ADAPTER_RECORD_DIGEST" not in output
    assert set(output) == {
        "MCP_RECORD_DIGEST",
        "ERP_RECORD_DIGEST",
        "OPENAI_RECORD_DIGEST",
        "COMMERCE_SALES_CATALOG_RECORD_DIGEST",
        "INVENTORY_CATALOG_RECORD_DIGEST",
        "SUPPLY_CHAIN_AGENT_RECORD_DIGEST",
        "SUPPLY_CHAIN_PLANNING_RECORD_DIGEST",
    }
    with pytest.raises(ValueError, match="incomplete|extras"):
        attestation.build_attestation_environment(descriptors(crm=True))


def test_explicit_crm_attestation_adds_only_the_exact_crm_digest() -> None:
    old = attestation.build_attestation_environment(descriptors())
    current = attestation.build_attestation_environment(descriptors(crm=True), include_crm=True)
    assert current == {**old, "CRM_ADAPTER_RECORD_DIGEST": "a" * 64}


@pytest.mark.parametrize("kind", ["missing", "extra", "duplicate"])
def test_crm_set_refuses_missing_extra_or_duplicate_provider(kind: str) -> None:
    values = descriptors(crm=True)
    if kind == "missing":
        values = values[:-1]
    elif kind == "duplicate":
        values += (values[-1],)
    else:
        values += (SimpleNamespace(**{**vars(values[-1]), "name": "unknown.provider"}),)
    with pytest.raises(ValueError, match="incomplete|extras"):
        attestation.build_attestation_environment(values, include_crm=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("distribution_name", "unapproved-package"),
        ("value", "foreign:Factory"),
        ("api_version", "v2"),
        ("distribution_version", ""),
        ("distribution_digest", "not-a-digest"),
        ("production_eligible", False),
    ],
)
def test_crm_descriptor_identity_is_not_relaxed(field: str, value: object) -> None:
    values = descriptors(crm=True)
    setattr(values[-1], field, value)
    with pytest.raises(ValueError, match="not production eligible"):
        attestation.build_attestation_environment(values, include_crm=True)


def test_cli_requires_explicit_crm_switch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        attestation, "discover_provider_factory_descriptors", lambda: descriptors(crm=True)
    )
    with pytest.raises(ValueError, match="incomplete|extras"):
        attestation.main([])
    assert attestation.main(["--with-crm"]) == 0
    assert json.loads(capsys.readouterr().out)["CRM_ADAPTER_RECORD_DIGEST"] == "a" * 64
