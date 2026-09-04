from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from agent_runtime.plugins.discovery import ProviderPluginDescriptor, discover_provider_entry_points
from agent_runtime.plugins.registry import PluginRegistry
from base_ai.providers import discover_provider_factory_descriptors
from test_config import deployment_document, deployment_env, write_runtime_policy

from ebiz_deployment.attestation import (
    attest_installed_supply_chain,
    attest_installed_supply_chain_planning,
)
from ebiz_deployment.composition import build_provider_composition
from ebiz_deployment.config import load_deployment_config

EXPECTED_PROVIDERS = {
    "mcp.streamable_http",
    "openai.responses",
    "yeaher.erp",
}
_PLANNING_PLUGIN_ID = "supply-chain-planning"
_LOCAL_FIXTURE_PLUGIN_ID = "deployment.fixture.governed-artifact"


def _production_plugin_descriptors() -> Sequence[ProviderPluginDescriptor]:
    """Model a clean production plugin set even when this test venv also has the local fixture."""

    descriptors = tuple(discover_provider_entry_points())
    by_id = {descriptor.name: descriptor for descriptor in descriptors}
    if set(by_id) - {_PLANNING_PLUGIN_ID, _LOCAL_FIXTURE_PLUGIN_ID}:
        raise AssertionError("clean composition discovered an unexpected Runtime plugin")
    fixture = by_id.get(_LOCAL_FIXTURE_PLUGIN_ID)
    if fixture is not None:
        assert fixture.distribution_name == "ebiz-deployment-local-evidence-fixture"
        assert fixture.value == "ebiz_deployment_local_fixture.plugin:factory"
    planning = by_id.get(_PLANNING_PLUGIN_ID)
    if planning is None:
        raise AssertionError("clean composition is missing the production Supply Chain plugin")
    return (planning,)


def test_production_plugin_discovery_excludes_only_the_known_local_fixture() -> None:
    assert [item.name for item in _production_plugin_descriptors()] == [_PLANNING_PLUGIN_ID]


@pytest.mark.asyncio
async def test_clean_wheels_start_three_real_providers_and_close_them(tmp_path: Path) -> None:
    descriptors = tuple(discover_provider_factory_descriptors())
    assert {item.name for item in descriptors} == EXPECTED_PROVIDERS
    assert all(item.production_eligible is True for item in descriptors)

    supply_chain = attest_installed_supply_chain()
    planning = attest_installed_supply_chain_planning()
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    runtime_policy = tmp_path / "runtime-policy.json"
    write_runtime_policy(runtime_policy, skill_root)
    config_path = tmp_path / "deployment.json"
    document = deployment_document(runtime_policy)
    by_id = {item.name: item for item in descriptors}
    for provider in document["base_ai_providers"]:  # type: ignore[index]
        descriptor = by_id[provider["provider_id"]]  # type: ignore[index]
        provider["package_version"] = descriptor.distribution_version  # type: ignore[index]
    config_path.write_text(json.dumps(document), encoding="utf-8")
    environ = deployment_env(runtime_policy)
    environ.update(
        {
            "MCP_RECORD_DIGEST": by_id["mcp.streamable_http"].distribution_digest,
            "ERP_RECORD_DIGEST": by_id["yeaher.erp"].distribution_digest,
            "OPENAI_RECORD_DIGEST": by_id["openai.responses"].distribution_digest,
            "SUPPLY_CHAIN_AGENT_RECORD_DIGEST": supply_chain.canonical_digest,
            "SUPPLY_CHAIN_PLANNING_RECORD_DIGEST": planning.canonical_digest,
        }
    )
    config = load_deployment_config(config_path, environ)
    artifacts = build_provider_composition(config, environ)

    # Other test modules import contract helpers during collection. Production
    # plugin startup occurs in a fresh interpreter, so recreate that boundary
    # before exercising descriptor re-attestation and entry-point loading.
    for module_name in tuple(sys.modules):
        if module_name == "ebiz_capability_supply_chain" or module_name.startswith(
            "ebiz_capability_supply_chain."
        ):
            sys.modules.pop(module_name, None)

    agent_registry = PluginRegistry(
        policy=config.runtime_plugin_policy,
        supported_api_version=config.runtime.supported_api_version,
        discoverer=_production_plugin_descriptors,
    )
    agent_snapshot = await agent_registry.load_startup()
    composition = await artifacts.root.start()

    assert len(agent_snapshot.plugins) == 1
    assert agent_snapshot.plugins[0].package_digest == planning.canonical_digest
    assert len(agent_snapshot.providers) == 9
    assert all(binding.plugin == agent_snapshot.plugins[0] for binding in agent_snapshot.providers)
    assert agent_registry.health_report.healthy is True
    assert len(composition.snapshot.plugins) == 3
    assert {item.provider_id for item in composition.snapshot.providers} == EXPECTED_PROVIDERS
    assert composition.health_report.startup_complete is True
    assert composition.health_report.healthy is True
    assert len(composition.health_report.providers) == 3
    openai = next(
        item for item in composition.snapshot.providers if item.provider_id == "openai.responses"
    )
    assert openai.registration.permissions == frozenset({"responses.create_structured"})
    assert all(
        [await item.registration.provider.health_check() for item in composition.snapshot.providers]
    )

    close_order: list[str] = []
    for binding in composition.snapshot.providers:
        real_provider = binding.registration.provider._provider  # type: ignore[attr-defined]
        original_close = real_provider.aclose

        async def close_and_record(
            provider_id: str = binding.provider_id,
            close: object = original_close,
        ) -> None:
            close_order.append(provider_id)
            await close()  # type: ignore[operator]

        real_provider.aclose = close_and_record

    await composition.aclose()

    assert close_order == ["openai.responses", "yeaher.erp", "mcp.streamable_http"]
    assert all(
        [
            not await item.registration.provider.health_check()
            for item in composition.snapshot.providers
        ]
    )


def test_two_launcher_processes_do_not_create_unrecorded_agent_bytecode(tmp_path: Path) -> None:
    descriptors = tuple(discover_provider_factory_descriptors())
    supply_chain = attest_installed_supply_chain()
    planning = attest_installed_supply_chain_planning()
    skill_root = tmp_path / "skills-restart"
    skill_root.mkdir()
    runtime_policy = tmp_path / "runtime-policy-restart.json"
    write_runtime_policy(runtime_policy, skill_root)
    config_path = tmp_path / "deployment-restart.json"
    document = deployment_document(runtime_policy)
    by_id = {item.name: item for item in descriptors}
    for provider in document["base_ai_providers"]:  # type: ignore[index]
        descriptor = by_id[provider["provider_id"]]  # type: ignore[index]
        provider["package_version"] = descriptor.distribution_version  # type: ignore[index]
    config_path.write_text(json.dumps(document), encoding="utf-8")
    child_environment = dict(os.environ)
    child_environment.update(deployment_env(runtime_policy))
    child_environment.update(
        {
            "EBIZ_DEPLOYMENT_CONFIG": str(config_path),
            "MCP_RECORD_DIGEST": by_id["mcp.streamable_http"].distribution_digest,
            "ERP_RECORD_DIGEST": by_id["yeaher.erp"].distribution_digest,
            "OPENAI_RECORD_DIGEST": by_id["openai.responses"].distribution_digest,
            "SUPPLY_CHAIN_AGENT_RECORD_DIGEST": supply_chain.canonical_digest,
            "SUPPLY_CHAIN_PLANNING_RECORD_DIGEST": planning.canonical_digest,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    script = r"""
import asyncio
import os
import sys
from importlib import metadata
from pathlib import Path

from agent_runtime.plugins.registry import PluginRegistry
from agent_runtime.plugins.discovery import discover_provider_entry_points
from ebiz_deployment.config import load_deployment_config
from ebiz_deployment.launcher import launch

assert sys.dont_write_bytecode is True
distribution = metadata.distribution("ebiz-capability-supply-chain")
root = Path(distribution.locate_file("ebiz_capability_supply_chain")).resolve(strict=True)
assert not tuple(root.rglob("*.pyc"))
assert not tuple(root.rglob("__pycache__"))


def runtime_main(argv, *, provider_composition):
    del argv, provider_composition
    config = load_deployment_config(Path(os.environ["EBIZ_DEPLOYMENT_CONFIG"]), os.environ)
    descriptors = tuple(discover_provider_entry_points())
    unexpected = {item.name for item in descriptors} - {
        "supply-chain-planning",
        "deployment.fixture.governed-artifact",
    }
    assert not unexpected
    local_fixture = next(
        (item for item in descriptors if item.name == "deployment.fixture.governed-artifact"),
        None,
    )
    if local_fixture is not None:
        assert local_fixture.distribution_name == "ebiz-deployment-local-evidence-fixture"
        assert local_fixture.value == "ebiz_deployment_local_fixture.plugin:factory"
    production = tuple(item for item in descriptors if item.name == "supply-chain-planning")
    assert len(production) == 1
    registry = PluginRegistry(
        policy=config.runtime_plugin_policy,
        supported_api_version=config.runtime.supported_api_version,
        discoverer=lambda: production,
    )
    snapshot = asyncio.run(registry.load_startup())
    assert len(snapshot.plugins) == 1
    assert len(snapshot.providers) == 9
    return 0


assert launch(["--check"], runtime_main=runtime_main) == 0
assert not tuple(root.rglob("*.pyc"))
assert not tuple(root.rglob("__pycache__"))
print("clean-start")
"""
    command = [sys.executable, "-c", script]
    assert "-B" not in command

    for _ in range(2):
        completed = subprocess.run(
            command,
            cwd=tmp_path,
            env=child_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert completed.stdout.strip() == "clean-start"
