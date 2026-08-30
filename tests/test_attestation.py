from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SUPPLY_CHAIN_PROJECT = Path("C:/ebizhub/worktrees/ebiz-agents-supply-chain-v4/agents/supply-chain")
PLANNING_PROJECT = Path(
    "C:/ebizhub/worktrees/ebiz-agents-supply-chain-v4/capabilities/supply-chain"
)
UV_COMMAND = Path(os.environ.get("UV_EXECUTABLE", shutil.which("uv") or "uv"))


def run_uv(*arguments: str) -> None:
    subprocess.run(
        [str(UV_COMMAND), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def clean_supply_chain_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("supply-chain-clean-wheel")
    wheel_dir = root / "wheel"
    site_packages = root / "site-packages"
    wheel_dir.mkdir()
    site_packages.mkdir()
    run_uv(
        "build",
        "--project",
        str(SUPPLY_CHAIN_PROJECT),
        "--out-dir",
        str(wheel_dir),
        "--no-sources",
    )
    wheel = next(wheel_dir.glob("ebiz_agent_inventory_supply_chain-*.whl"))
    run_uv(
        "pip",
        "install",
        "--target",
        str(site_packages),
        "--no-deps",
        str(wheel),
    )
    return site_packages


@pytest.fixture(scope="session")
def clean_planning_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("supply-chain-planning-clean-wheel")
    wheel_dir = root / "wheel"
    site_packages = root / "site-packages"
    wheel_dir.mkdir()
    site_packages.mkdir()
    run_uv("build", "--project", str(PLANNING_PROJECT), "--out-dir", str(wheel_dir), "--no-sources")
    wheel = next(wheel_dir.glob("ebiz_capability_supply_chain-*.whl"))
    run_uv("pip", "install", "--target", str(site_packages), "--no-deps", str(wheel))
    return site_packages


def copied_site(clean_supply_chain_site: Path, tmp_path: Path) -> Path:
    site = tmp_path / "site-packages"
    shutil.copytree(clean_supply_chain_site, site)
    return site


def dist_info(site: Path) -> Path:
    return next(site.glob("ebiz_agent_inventory_supply_chain-*.dist-info"))


def rewrite_record(site: Path, *, extra_paths: tuple[str, ...] = ()) -> None:
    record = dist_info(site) / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"), newline="")))
    paths = [row[0] for row in rows if row[0] != record.relative_to(site).as_posix()]
    paths.extend(extra_paths)
    normalized: list[tuple[str, str, str]] = []
    for relative in sorted(set(paths)):
        content = (site / Path(*relative.split("/"))).read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        normalized.append((relative, f"sha256={digest}", str(len(content))))
    normalized.append((record.relative_to(site).as_posix(), "", ""))
    with record.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(normalized)


def expected_canonical_digest(site: Path) -> str:
    record = dist_info(site) / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"), newline="")))
    normalized = sorted((row[0], row[1], row[2]) for row in rows)
    canonical = json.dumps(normalized, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def real_adapter_descriptors() -> tuple[SimpleNamespace, ...]:
    return tuple(
        SimpleNamespace(
            name=provider_id,
            distribution_name=package_name,
            distribution_version="0.1.0",
            distribution_digest=digest,
            value=value,
            api_version=api_version,
            production_eligible=True,
        )
        for provider_id, package_name, digest, value, api_version in (
            (
                "mcp.streamable_http",
                "ebiz-adapter-mcp",
                "a" * 64,
                "ebiz_adapter_mcp:McpProviderFactory",
                "streamable-http/1",
            ),
            (
                "yeaher.erp",
                "ebiz-adapter-erp",
                "b" * 64,
                "ebiz_adapter_erp:ErpProviderFactory",
                "v1",
            ),
            (
                "openai.responses",
                "ebiz-adapter-model-openai",
                "c" * 64,
                "ebiz_adapter_model_openai:OpenAIProviderFactory",
                "responses/v1",
            ),
        )
    )


def attest(module: Any, site: Path) -> Any:
    return module.attest_installed_supply_chain(search_paths=(site,))


def test_attests_real_non_editable_supply_chain_wheel(clean_supply_chain_site: Path) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")

    result = attest(module, clean_supply_chain_site)

    assert result.distribution_name == "ebiz-agent-inventory-supply-chain"
    assert result.distribution_version == "4.0.0"
    assert result.entry_point_group is None
    assert result.entry_point_name is None
    assert result.entry_point_value is None
    assert result.import_root == "inventory_supply_chain_agent"
    assert result.canonical_digest == expected_canonical_digest(clean_supply_chain_site)


def test_attests_public_planning_provider_wheel(clean_planning_site: Path) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")

    result = module.attest_installed_supply_chain_planning(search_paths=(clean_planning_site,))

    assert result.distribution_name == "ebiz-capability-supply-chain"
    assert result.distribution_version == "2.0.0"
    assert result.entry_point_group == "ebiz_agents.providers"
    assert result.entry_point_name == "supply-chain-planning"
    assert result.entry_point_value == "ebiz_capability_supply_chain.plugin:factory"
    assert result.import_root == "ebiz_capability_supply_chain"


def test_environment_uses_installed_supply_chain_digest_not_a_caller_value(
    clean_supply_chain_site: Path,
) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")

    result = module.build_attestation_environment(
        real_adapter_descriptors(), supply_chain_search_paths=(clean_supply_chain_site,)
    )

    assert result == {
        "COMMERCE_SALES_CATALOG_RECORD_DIGEST": (
            module.attest_installed_commerce_sales_catalog().canonical_digest
        ),
        "ERP_RECORD_DIGEST": "b" * 64,
        "INVENTORY_CATALOG_RECORD_DIGEST": (
            module.attest_installed_inventory_catalog().canonical_digest
        ),
        "MCP_RECORD_DIGEST": "a" * 64,
        "OPENAI_RECORD_DIGEST": "c" * 64,
        "SUPPLY_CHAIN_AGENT_RECORD_DIGEST": expected_canonical_digest(clean_supply_chain_site),
        "SUPPLY_CHAIN_PLANNING_RECORD_DIGEST": (
            module.attest_installed_supply_chain_planning().canonical_digest
        ),
    }


def test_rejects_editable_supply_chain_distribution(
    clean_supply_chain_site: Path, tmp_path: Path
) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")
    site = copied_site(clean_supply_chain_site, tmp_path)
    direct_url = dist_info(site) / "direct_url.json"
    direct_url.write_text(
        json.dumps({"url": "file:///workspace", "dir_info": {"editable": True}}),
        encoding="utf-8",
    )
    rewrite_record(site, extra_paths=(direct_url.relative_to(site).as_posix(),))

    with pytest.raises(ValueError, match="attestation"):
        attest(module, site)


def test_rejects_tampered_recorded_supply_chain_file(
    clean_supply_chain_site: Path, tmp_path: Path
) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")
    site = copied_site(clean_supply_chain_site, tmp_path)
    (site / "inventory_supply_chain_agent" / "__init__.py").write_text(
        "raise RuntimeError('tampered')\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="attestation"):
        attest(module, site)


def test_rejects_unrecorded_import_shadow(clean_supply_chain_site: Path, tmp_path: Path) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")
    site = copied_site(clean_supply_chain_site, tmp_path)
    shadow = site / "inventory_supply_chain_agent" / "factory.py"
    shadow.write_text("factory = object()\n", encoding="utf-8")

    with pytest.raises(ValueError, match="attestation"):
        attest(module, site)


def test_rejects_symlink_inside_import_root(clean_supply_chain_site: Path, tmp_path: Path) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")
    site = copied_site(clean_supply_chain_site, tmp_path)
    source = site / "inventory_supply_chain_agent" / "__init__.py"
    shadow = site / "inventory_supply_chain_agent" / "shadow.py"
    try:
        shadow.symlink_to(source)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error.winerror}")

    with pytest.raises(ValueError, match="attestation"):
        attest(module, site)


@pytest.mark.parametrize(
    ("relative_path", "replacement"),
    [
        (
            "ebiz_agent_inventory_supply_chain-4.0.0.dist-info/METADATA",
            ("Name: ebiz-agent-inventory-supply-chain", "Name: wrong-package"),
        ),
        (
            "ebiz_agent_inventory_supply_chain-4.0.0.dist-info/METADATA",
            ("Version: 4.0.0", "Version: 9.9.9"),
        ),
    ],
)
def test_rejects_wrong_distribution_or_entrypoint_identity(
    clean_supply_chain_site: Path,
    tmp_path: Path,
    relative_path: str,
    replacement: tuple[str, str],
) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")
    site = copied_site(clean_supply_chain_site, tmp_path)
    target = site / Path(*relative_path.split("/"))
    target.write_text(target.read_text(encoding="utf-8").replace(*replacement), encoding="utf-8")
    rewrite_record(site)

    with pytest.raises(ValueError, match="attestation"):
        attest(module, site)


def test_rejects_invalid_record_path(clean_supply_chain_site: Path, tmp_path: Path) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")
    site = copied_site(clean_supply_chain_site, tmp_path)
    record = dist_info(site) / "RECORD"
    with record.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerow(
            ("../escape.py", "sha256=" + "a" * 43, "1")
        )

    with pytest.raises(ValueError, match="attestation"):
        attest(module, site)


def test_rejects_editable_or_incomplete_base_ai_attestation_set(
    clean_supply_chain_site: Path,
) -> None:
    module = importlib.import_module("ebiz_deployment.attestation")
    descriptor = real_adapter_descriptors()[0]
    descriptor.production_eligible = False

    with pytest.raises(ValueError, match="attestation"):
        module.build_attestation_environment(
            (descriptor,), supply_chain_search_paths=(clean_supply_chain_site,)
        )
