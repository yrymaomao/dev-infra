"""Deterministic, wheel-only Runtime 0.1.6 release composition."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_COMMIT = re.compile(r"^[a-f0-9]{7,40}$")
_SECRET_REF = re.compile(r"^(?:kms|secret)://[A-Za-z0-9._/:-]+$")
_BUNDLE_ID = "runtime-model-seam-0.1.6"
_EXPECTED_ARTIFACTS = {
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
_CRM_GATES = (
    "migration",
    "shared_mysql_replay",
    "profile_digest",
    "keyring",
    "readiness",
)


class ReleaseBundleError(ValueError):
    """The release bundle is incomplete, mutable, or source-resolved."""


@dataclass(frozen=True, slots=True)
class ArtifactPin:
    distribution: str
    version: str
    wheel_filename: str
    sha256: str
    commit: str


@dataclass(frozen=True, slots=True)
class MaterialPin:
    kind: str
    name: str
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ActorKeyring:
    issuer: str
    audience: str
    profile: str
    profile_digest: str
    active_kid: str
    active_key_ref: str
    previous_kid: str | None
    previous_key_ref: str | None
    switched_at_epoch_seconds: int
    previous_retire_after_epoch_seconds: int | None
    assertion_ttl_seconds: int = 60
    clock_skew_seconds: int = 5


@dataclass(frozen=True, slots=True)
class ReleaseReadiness:
    migration: bool
    shared_mysql_replay: bool
    profile_digest: bool
    keyring: bool
    readiness: bool


@dataclass(frozen=True, slots=True)
class ReleaseBundle:
    bundle_id: str
    composition_version: str
    artifacts: tuple[ArtifactPin, ...]
    materials: tuple[MaterialPin, ...]
    venv_digest: str
    keyring: ActorKeyring
    crm_readiness: ReleaseReadiness


@dataclass(frozen=True, slots=True)
class ActivationPlan:
    model_seam: bool
    manifest_profiles: bool
    crm_read: bool
    crm_write: bool
    crm_write_blockers: tuple[str, ...]


def load_release_bundle(path: Path | str) -> ReleaseBundle:
    """Load and validate a secret-free, immutable release bundle."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ReleaseBundleError("release bundle is missing or malformed") from None
    if not isinstance(raw, dict):
        raise ReleaseBundleError("release bundle must be an object")
    allowed = {
        "bundle_id",
        "composition_version",
        "artifacts",
        "materials",
        "venv_digest",
        "keyring",
        "crm_readiness",
    }
    if set(raw) != allowed:
        raise ReleaseBundleError("release bundle fields are incomplete or unknown")
    try:
        artifacts = tuple(ArtifactPin(**item) for item in _object_list(raw["artifacts"]))
        materials = tuple(MaterialPin(**item) for item in _object_list(raw["materials"]))
        keyring = ActorKeyring(**_object(raw["keyring"], "keyring"))
        readiness = ReleaseReadiness(**_object(raw["crm_readiness"], "crm_readiness"))
        bundle = ReleaseBundle(
            bundle_id=str(raw["bundle_id"]),
            composition_version=str(raw["composition_version"]),
            artifacts=artifacts,
            materials=materials,
            venv_digest=str(raw["venv_digest"]),
            keyring=keyring,
            crm_readiness=readiness,
        )
    except (TypeError, KeyError):
        raise ReleaseBundleError("release bundle records are incomplete or unknown") from None
    _validate_bundle(bundle)
    return bundle


def activation_plan(bundle: ReleaseBundle) -> ActivationPlan:
    """Fail closed only for CRM WRITE when a CRM joint gate is incomplete."""

    readiness = asdict(bundle.crm_readiness)
    blockers = tuple(name for name in _CRM_GATES if readiness[name] is not True)
    return ActivationPlan(
        model_seam=True,
        manifest_profiles=True,
        crm_read=True,
        crm_write=not blockers,
        crm_write_blockers=blockers,
    )


def verify_wheelhouse(bundle: ReleaseBundle, wheelhouse: Path | str) -> None:
    """Verify every pin against exact wheel bytes; no source fallback is allowed."""

    root = Path(wheelhouse).resolve()
    if not root.is_dir():
        raise ReleaseBundleError("wheelhouse must be a readable directory")
    expected = {pin.wheel_filename for pin in bundle.artifacts}
    actual = {item.name for item in root.iterdir() if item.is_file() and item.suffix == ".whl"}
    if actual != expected:
        raise ReleaseBundleError("wheelhouse must contain exactly the pinned wheels")
    for pin in bundle.artifacts:
        wheel = root / pin.wheel_filename
        if wheel.is_symlink() or hashlib.sha256(wheel.read_bytes()).hexdigest() != pin.sha256:
            raise ReleaseBundleError(f"wheel digest mismatch: {pin.distribution}")


def verify_release_environment(environ: Mapping[str, str]) -> None:
    """Reject workspace/source injection at the production assembly boundary."""

    if environ.get("PYTHONPATH", "").strip():
        raise ReleaseBundleError("PYTHONPATH must be empty for release assembly")
    if environ.get("UV_PROJECT_ENVIRONMENT", "").strip():
        raise ReleaseBundleError("project environments cannot be used for release assembly")


def write_release_outputs(
    bundle: ReleaseBundle,
    output_dir: Path | str,
    *,
    migration_report: Mapping[str, Any],
    cross_repository_tests: Sequence[Mapping[str, Any]],
) -> tuple[Path, ...]:
    """Write deterministic promotion materials after all joint gates pass."""

    plan = activation_plan(bundle)
    if not plan.crm_write:
        raise ReleaseBundleError("joint gate incomplete; final release outputs are blocked")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = _bundle_document(bundle)
    files: dict[str, bytes] = {
        "release-manifest.json": _json_bytes(manifest),
        "migration-report.json": _json_bytes(dict(migration_report)),
        "cross-repository-tests.json": _json_bytes(list(cross_repository_tests)),
        "sbom.spdx.json": _json_bytes(_spdx_document(bundle)),
        "rollback.md": _rollback_document(bundle).encode("utf-8"),
    }
    for name, content in files.items():
        (root / name).write_bytes(content)
    checksum_lines = [
        f"{hashlib.sha256(content).hexdigest()}  {name}" for name, content in sorted(files.items())
    ]
    files["SHA256SUMS"] = ("\n".join(checksum_lines) + "\n").encode("ascii")
    (root / "SHA256SUMS").write_bytes(files["SHA256SUMS"])
    return tuple(root / name for name in sorted(files))


def main(argv: list[str] | None = None) -> int:
    """Validate an immutable bundle and its exact wheelhouse."""

    parser = argparse.ArgumentParser(description="Validate Runtime 0.1.6 release composition")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    arguments = parser.parse_args(argv)
    verify_release_environment(os.environ)
    bundle = load_release_bundle(arguments.bundle)
    verify_wheelhouse(bundle, arguments.wheelhouse)
    plan = activation_plan(bundle)
    print(
        json.dumps(
            {
                "bundle_id": bundle.bundle_id,
                "crm_write": plan.crm_write,
                "crm_write_blockers": plan.crm_write_blockers,
            },
            sort_keys=True,
        )
    )
    return 0


def _validate_bundle(bundle: ReleaseBundle) -> None:
    if bundle.bundle_id != _BUNDLE_ID or bundle.composition_version != "0.1.1":
        raise ReleaseBundleError("release identity differs from Runtime 0.1.6")
    by_distribution = {pin.distribution: pin for pin in bundle.artifacts}
    if (
        len(by_distribution) != len(bundle.artifacts)
        or {name: pin.version for name, pin in by_distribution.items()} != _EXPECTED_ARTIFACTS
    ):
        raise ReleaseBundleError("artifact set or versions differ from the fixed composition")
    for pin in bundle.artifacts:
        if (
            Path(pin.wheel_filename).name != pin.wheel_filename
            or not pin.wheel_filename.endswith(".whl")
            or not _SHA256.fullmatch(pin.sha256)
            or not _COMMIT.fullmatch(pin.commit)
        ):
            raise ReleaseBundleError("artifact pin is not an immutable wheel identity")
    material_keys = {(item.kind, item.name) for item in bundle.materials}
    required_kinds = {"schema", "registry", "configuration"}
    if len(material_keys) != len(bundle.materials) or not required_kinds.issubset(
        {item.kind for item in bundle.materials}
    ):
        raise ReleaseBundleError("schema, registry, and configuration materials are required")
    if any(not _SHA256.fullmatch(item.sha256) for item in bundle.materials):
        raise ReleaseBundleError("material digest must be lowercase SHA-256")
    if not _SHA256.fullmatch(bundle.venv_digest):
        raise ReleaseBundleError("venv_digest must be lowercase SHA-256")
    _validate_keyring(bundle.keyring)
    if any(type(value) is not bool for value in asdict(bundle.crm_readiness).values()):
        raise ReleaseBundleError("CRM readiness gates must be booleans")


def _validate_keyring(keyring: ActorKeyring) -> None:
    if (
        not all(
            isinstance(value, str)
            for value in (
                keyring.issuer,
                keyring.audience,
                keyring.profile,
                keyring.profile_digest,
                keyring.active_kid,
                keyring.active_key_ref,
            )
        )
        or type(keyring.switched_at_epoch_seconds) is not int
        or keyring.profile != "crm-confirm-action/v1"
        or keyring.assertion_ttl_seconds != 60
        or keyring.clock_skew_seconds != 5
        or not _SHA256.fullmatch(keyring.profile_digest)
        or not _SECRET_REF.fullmatch(keyring.active_key_ref)
    ):
        raise ReleaseBundleError("actor assertion keyring/profile is not release compatible")
    previous = (
        keyring.previous_kid,
        keyring.previous_key_ref,
        keyring.previous_retire_after_epoch_seconds,
    )
    if any(value is not None for value in previous):
        if any(value is None for value in previous):
            raise ReleaseBundleError("previous key configuration must be complete")
        assert keyring.previous_key_ref is not None
        assert keyring.previous_retire_after_epoch_seconds is not None
        if (
            not isinstance(keyring.previous_kid, str)
            or not isinstance(keyring.previous_key_ref, str)
            or type(keyring.previous_retire_after_epoch_seconds) is not int
        ):
            raise ReleaseBundleError("previous key configuration has invalid types")
        if (
            keyring.previous_kid == keyring.active_kid
            or not _SECRET_REF.fullmatch(keyring.previous_key_ref)
            or keyring.previous_retire_after_epoch_seconds < keyring.switched_at_epoch_seconds + 65
        ):
            raise ReleaseBundleError("previous key must remain distinct and valid for 65 seconds")


def _bundle_document(bundle: ReleaseBundle) -> dict[str, Any]:
    document = asdict(bundle)
    document["artifacts"] = sorted(document["artifacts"], key=lambda item: item["distribution"])
    document["materials"] = sorted(
        document["materials"], key=lambda item: (item["kind"], item["name"])
    )
    document["activation"] = asdict(activation_plan(bundle))
    return document


def _spdx_document(bundle: ReleaseBundle) -> dict[str, Any]:
    return {
        "SPDXID": "SPDXRef-DOCUMENT",
        "dataLicense": "CC0-1.0",
        "name": bundle.bundle_id,
        "spdxVersion": "SPDX-2.3",
        "packages": [
            {
                "SPDXID": f"SPDXRef-{pin.distribution}",
                "checksums": [{"algorithm": "SHA256", "checksumValue": pin.sha256}],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "name": pin.distribution,
                "versionInfo": pin.version,
            }
            for pin in sorted(bundle.artifacts, key=lambda item: item.distribution)
        ],
    }


def _rollback_document(bundle: ReleaseBundle) -> str:
    return (
        f"# Rollback: {bundle.bundle_id}\n\n"
        "Disable CRM WRITE first. Preserve the replay table, ApprovalAuthoritySnapshot, "
        "attempt, usage, and reconciliation ledgers. Restore the prior immutable Release "
        "Snapshot; do not delete migrations or release pending monetary reservations.\n"
    )


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseBundleError(f"{label} must be an object")
    return value


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ReleaseBundleError("release records must be object arrays")
    return value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


__all__ = [
    "ActivationPlan",
    "ActorKeyring",
    "ArtifactPin",
    "MaterialPin",
    "ReleaseBundle",
    "ReleaseBundleError",
    "ReleaseReadiness",
    "activation_plan",
    "load_release_bundle",
    "main",
    "verify_release_environment",
    "verify_wheelhouse",
    "write_release_outputs",
]
