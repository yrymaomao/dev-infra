from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from agent_runtime.base_ai.composition import BaseAICompositionRoot
from test_config import deployment_document, deployment_env, write_runtime_policy


def composition_module() -> Any:
    return importlib.import_module("ebiz_deployment.composition")


def launcher_module() -> Any:
    return importlib.import_module("ebiz_deployment.launcher")


def configured_files(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    runtime_policy = tmp_path / "runtime-policy.json"
    write_runtime_policy(runtime_policy, skill_root)
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(deployment_document(runtime_policy)), encoding="utf-8")
    environ = deployment_env(runtime_policy)
    environ["EBIZ_DEPLOYMENT_CONFIG"] = str(config_path)
    return config_path, runtime_policy, environ


def test_builds_three_provider_root_and_exact_base_ai_policy(tmp_path: Path) -> None:
    config_path, runtime_policy, environ = configured_files(tmp_path)
    config = importlib.import_module("ebiz_deployment.config").load_deployment_config(
        config_path, environ
    )

    result = composition_module().build_provider_composition(config, environ)

    assert isinstance(result.root, BaseAICompositionRoot)
    assert [plugin.plugin_id for plugin in result.base_ai_policy.plugins] == [
        "base-ai.mcp.streamable_http",
        "base-ai.openai.responses",
        "base-ai.yeaher.erp",
    ]
    assert result.runtime_plugin_policy.plugins[0].package_digest == "d" * 64
    by_id = {deployment.provider_id: deployment for deployment in result.deployments}
    assert by_id["mcp.streamable_http"].config["allowed_tools"] == [
        "query_fba_inventory_snapshot_v1",
        "query_inventory_batch_snapshot_v1",
        "query_inventory_skus_by_threshold_v1",
        "query_inventory_summary_v2",
        "query_sku_boston_cohort_v1",
        "query_sku_fulfillment_sales_profit_windows_v2",
        "query_sku_identity_mapping_v1",
        "query_sku_sales_profit_windows_v1",
        "query_sku_upc_mapping",
    ]
    assert by_id["yeaher.erp"].enabled_operations == (
        "catalog.resolve_sku_identity",
        "catalog.resolve_sku_identity_batch",
        "inventory.get_batch_snapshot",
        "inventory.get_fba_snapshot",
        "inventory.get_total_snapshot",
        "inventory.list_skus_by_threshold",
        "sales_profit.get_boston_cohort",
        "sales_profit.get_sku_fulfillment_windows",
        "sales_profit.get_sku_windows",
    )
    assert by_id["yeaher.erp"].egress_hosts == ()
    assert by_id["yeaher.erp"].config["mcp"]["tools"] == {
        "catalog.resolve_sku_identity": "query_sku_upc_mapping",
        "catalog.resolve_sku_identity_batch": "query_sku_identity_mapping_v1",
        "inventory.get_fba_snapshot": "query_fba_inventory_snapshot_v1",
        "inventory.get_batch_snapshot": "query_inventory_batch_snapshot_v1",
        "inventory.get_total_snapshot": "query_inventory_summary_v2",
        "inventory.list_skus_by_threshold": "query_inventory_skus_by_threshold_v1",
        "sales_profit.get_boston_cohort": "query_sku_boston_cohort_v1",
        "sales_profit.get_sku_fulfillment_windows": "query_sku_fulfillment_sales_profit_windows_v2",
        "sales_profit.get_sku_windows": "query_sku_sales_profit_windows_v1",
    }
    assert by_id["openai.responses"].enabled_operations == ("responses.create_structured",)
    assert str(runtime_policy.resolve()) == environ["APP_PLUGIN_POLICY_PATH"]


def test_missing_model_or_broker_secret_fails_before_composition(tmp_path: Path) -> None:
    config_path, _, environ = configured_files(tmp_path)
    config = importlib.import_module("ebiz_deployment.config").load_deployment_config(
        config_path, environ
    )
    del environ["DEPLOY_OPENAI_API_KEY"]

    with pytest.raises(ValueError) as error:
        composition_module().build_provider_composition(config, environ)

    assert "api_key" in str(error.value)
    assert "broker-secret-value" not in str(error.value)


def test_launcher_injects_composition_into_runtime_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    _, _, environ = configured_files(tmp_path)
    received: dict[str, object] = {}

    def runtime_main(argv: list[str], *, provider_composition: object) -> int:
        received["argv"] = argv
        received["provider_composition"] = provider_composition
        return 23

    result = launcher_module().launch(["--check"], environ=environ, runtime_main=runtime_main)

    assert result == 23
    assert received["argv"] == ["--check"]
    assert isinstance(received["provider_composition"], BaseAICompositionRoot)


def test_launcher_fails_closed_when_runtime_policy_path_disagrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    _, _, environ = configured_files(tmp_path)
    environ["APP_PLUGIN_POLICY_PATH"] = str(tmp_path / "other-policy.json")
    called = False

    def runtime_main(argv: list[str], *, provider_composition: object) -> int:
        del argv, provider_composition
        nonlocal called
        called = True
        return 0

    with pytest.raises(ValueError) as error:
        launcher_module().launch([], environ=environ, runtime_main=runtime_main)

    assert "APP_PLUGIN_POLICY_PATH" in str(error.value)
    assert called is False


def test_launcher_rejects_bytecode_writes_before_runtime_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, environ = configured_files(tmp_path)
    called = False

    def runtime_main(argv: list[str], *, provider_composition: object) -> int:
        del argv, provider_composition
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    with pytest.raises(ValueError, match="bytecode"):
        launcher_module().launch([], environ=environ, runtime_main=runtime_main)

    assert called is False
