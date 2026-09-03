from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ebiz_deployment.model_seam_release import (
    ReleaseBundleError,
    activation_plan,
    load_release_bundle,
    verify_release_environment,
    verify_wheelhouse,
    write_release_outputs,
)

VERSIONS = {
    "base-ai": "0.1.1",
    "ebiz-adapter-erp": "0.1.0",
    "ebiz-adapter-mcp": "0.1.0",
    "ebiz-adapter-model-openai": "0.1.1",
    "ebiz-agent-inventory-supply-chain": "4.0.0",
    "ebiz-capability-commerce-sales-catalog": "2.0.0",
    "ebiz-capability-inventory-catalog": "2.0.0",
    "ebiz-capability-supply-chain": "2.0.0",
    "ebiz-deployment-composition": "0.1.1",
    "ebiz-runtime-contracts": "0.1.6",
    "ebiz-workflow-runtime": "0.1.6",
    "ebizhub-agent-runtime": "0.1.6",
}


def bundle_document(*, crm_ready: bool = True) -> dict[str, object]:
    return {
        "bundle_id": "runtime-model-seam-0.1.6",
        "composition_version": "0.1.1",
        "artifacts": [
            {
                "distribution": name,
                "version": version,
                "wheel_filename": f"{name.replace('-', '_')}-{version}-py3-none-any.whl",
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
                "commit": "1234567890abcdef",
            }
            for name, version in VERSIONS.items()
        ],
        "materials": [
            {"kind": "schema", "name": "manifest-v1.1", "version": "1.1", "sha256": "a" * 64},
            {"kind": "registry", "name": "model-registry", "version": "1", "sha256": "b" * 64},
            {"kind": "configuration", "name": "runtime", "version": "0.1.6", "sha256": "c" * 64},
        ],
        "venv_digest": "d" * 64,
        "keyring": {
            "issuer": "ebizhub-agent-runtime",
            "audience": "crm-service",
            "profile": "crm-confirm-action/v1",
            "profile_digest": "e" * 64,
            "active_kid": "2026-09-active",
            "active_key_ref": "kms://agent-runtime/crm/active",
            "previous_kid": "2026-08-previous",
            "previous_key_ref": "secret://crm/assertion/previous",
            "switched_at_epoch_seconds": 1000,
            "previous_retire_after_epoch_seconds": 1065,
            "assertion_ttl_seconds": 60,
            "clock_skew_seconds": 5,
        },
        "crm_readiness": {
            "migration": crm_ready,
            "shared_mysql_replay": crm_ready,
            "profile_digest": crm_ready,
            "keyring": crm_ready,
            "readiness": crm_ready,
        },
    }


def write_bundle(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_loads_exact_release_and_enables_all_surfaces(tmp_path: Path) -> None:
    bundle = load_release_bundle(write_bundle(tmp_path, bundle_document()))

    assert [pin.distribution for pin in bundle.artifacts] == list(VERSIONS)
    assert activation_plan(bundle).crm_write is True
    assert activation_plan(bundle).crm_write_blockers == ()


def test_incomplete_crm_gate_blocks_only_crm_write(tmp_path: Path) -> None:
    document = bundle_document()
    document["crm_readiness"]["shared_mysql_replay"] = False  # type: ignore[index]
    bundle = load_release_bundle(write_bundle(tmp_path, document))

    plan = activation_plan(bundle)

    assert plan.model_seam is True
    assert plan.manifest_profiles is True
    assert plan.crm_read is True
    assert plan.crm_write is False
    assert plan.crm_write_blockers == ("shared_mysql_replay",)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["artifacts"].pop(),
        lambda value: value["artifacts"][0].update({"version": "0.1.0"}),
        lambda value: value["artifacts"][0].update({"wheel_filename": "C:/source/pkg.whl"}),
        lambda value: value["keyring"].update({"active_key_ref": "raw-secret"}),
        lambda value: value["keyring"].update({"previous_retire_after_epoch_seconds": 1064}),
        lambda value: value["crm_readiness"].update({"migration": "yes"}),
    ],
)
def test_rejects_drift_source_paths_raw_keys_and_short_rotation(
    tmp_path: Path, mutation: object
) -> None:
    document = bundle_document()
    mutation(document)  # type: ignore[operator]

    with pytest.raises(ReleaseBundleError):
        load_release_bundle(write_bundle(tmp_path, document))


def test_wheelhouse_requires_exact_named_bytes(tmp_path: Path) -> None:
    document = bundle_document()
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    for item in document["artifacts"]:  # type: ignore[union-attr]
        content = item["distribution"].encode()  # type: ignore[index]
        (wheelhouse / item["wheel_filename"]).write_bytes(content)  # type: ignore[index]
    bundle = load_release_bundle(write_bundle(tmp_path, document))

    verify_wheelhouse(bundle, wheelhouse)
    (wheelhouse / bundle.artifacts[0].wheel_filename).write_bytes(b"tampered")
    with pytest.raises(ReleaseBundleError, match="digest mismatch"):
        verify_wheelhouse(bundle, wheelhouse)


def test_release_environment_rejects_source_injection() -> None:
    verify_release_environment({})
    with pytest.raises(ReleaseBundleError, match="PYTHONPATH"):
        verify_release_environment({"PYTHONPATH": "C:/ebizhub/workspace"})
    with pytest.raises(ReleaseBundleError, match="project environments"):
        verify_release_environment({"UV_PROJECT_ENVIRONMENT": ".venv"})


def test_outputs_are_deterministic_and_require_joint_gate(tmp_path: Path) -> None:
    bundle = load_release_bundle(write_bundle(tmp_path, bundle_document()))
    first = tmp_path / "first"
    second = tmp_path / "second"
    kwargs = {
        "migration_report": {"crm": "passed", "runtime": "passed"},
        "cross_repository_tests": ({"repository": "runtime", "status": "passed"},),
    }

    first_files = write_release_outputs(bundle, first, **kwargs)
    second_files = write_release_outputs(bundle, second, **kwargs)

    assert [item.name for item in first_files] == [item.name for item in second_files]
    assert {item.name: item.read_bytes() for item in first_files} == {
        item.name: item.read_bytes() for item in second_files
    }
    assert {item.name for item in first_files} == {
        "SHA256SUMS",
        "cross-repository-tests.json",
        "migration-report.json",
        "release-manifest.json",
        "rollback.md",
        "sbom.spdx.json",
    }

    blocked = load_release_bundle(write_bundle(tmp_path, bundle_document(crm_ready=False)))
    with pytest.raises(ReleaseBundleError, match="joint gate"):
        write_release_outputs(blocked, tmp_path / "blocked", **kwargs)
