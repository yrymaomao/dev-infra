"""Independent canonical RECORD attestation for installed Python distributions."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path, PurePosixPath

CANONICAL_RECORD_ATTESTATION = "sha256-canonical-record-v1"

_RECORD_HASH = re.compile(r"^sha256=[A-Za-z0-9_-]{43}$")
_ENTRY_POINT_VALUE = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):"
    r"(?P<attribute>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)
_IMPORT_SUFFIXES = (".py", ".pyc", ".pyd", ".so", ".dll", ".dylib")


@dataclass(frozen=True, slots=True)
class InstalledDistributionAttestation:
    distribution_name: str
    distribution_version: str
    entry_point_group: str | None
    entry_point_name: str | None
    entry_point_value: str | None
    import_root: str
    canonical_digest: str
    attestation_algorithm: str = CANONICAL_RECORD_ATTESTATION


def attest_installed_distribution(
    *,
    distribution_name: str,
    distribution_version: str,
    entry_point_group: str,
    entry_point_name: str,
    entry_point_value: str,
    search_paths: Sequence[Path] | None = None,
) -> InstalledDistributionAttestation:
    """Attest one exact non-editable installed distribution without importing it."""

    try:
        distribution = _exact_distribution(distribution_name, search_paths)
        actual_name = distribution.metadata["Name"]
        actual_version = distribution.version
        if actual_name != distribution_name or actual_version != distribution_version:
            raise ValueError
        expected_entry_point = (entry_point_group, entry_point_name, entry_point_value)
        provider_entry_points = tuple(
            (item.group, item.name, item.value)
            for item in distribution.entry_points
            if item.group == entry_point_group
        )
        if provider_entry_points != (expected_entry_point,):
            raise ValueError
        match = _ENTRY_POINT_VALUE.fullmatch(entry_point_value)
        if match is None:
            raise ValueError
        import_root = match.group("module").partition(".")[0]
        digest, production_eligible = _verified_record_attestation(
            distribution, import_root=import_root
        )
        if not production_eligible:
            raise ValueError
        return InstalledDistributionAttestation(
            distribution_name=actual_name,
            distribution_version=actual_version,
            entry_point_group=entry_point_group,
            entry_point_name=entry_point_name,
            entry_point_value=entry_point_value,
            import_root=import_root,
            canonical_digest=digest,
        )
    except Exception:
        raise ValueError("installed distribution attestation failed") from None


def attest_installed_resource_distribution(
    *,
    distribution_name: str,
    distribution_version: str,
    import_root: str,
    forbidden_entry_point_group: str,
    search_paths: Sequence[Path] | None = None,
) -> InstalledDistributionAttestation:
    """Attest an immutable resource wheel that must not expose a Provider entry point."""

    try:
        distribution = _exact_distribution(distribution_name, search_paths)
        actual_name = distribution.metadata["Name"]
        actual_version = distribution.version
        if actual_name != distribution_name or actual_version != distribution_version:
            raise ValueError
        if any(item.group == forbidden_entry_point_group for item in distribution.entry_points):
            raise ValueError
        digest, production_eligible = _verified_record_attestation(
            distribution, import_root=import_root
        )
        if not production_eligible:
            raise ValueError
        return InstalledDistributionAttestation(
            distribution_name=actual_name,
            distribution_version=actual_version,
            entry_point_group=None,
            entry_point_name=None,
            entry_point_value=None,
            import_root=import_root,
            canonical_digest=digest,
        )
    except Exception:
        raise ValueError("installed distribution attestation failed") from None


def _exact_distribution(name: str, search_paths: Sequence[Path] | None) -> Distribution:
    installed = (
        distributions()
        if search_paths is None
        else distributions(path=[str(path.resolve()) for path in search_paths])
    )
    candidates = tuple(
        distribution for distribution in installed if distribution.metadata["Name"] == name
    )
    if len(candidates) != 1:
        raise ValueError
    return candidates[0]


def _verified_record_attestation(
    distribution: Distribution,
    *,
    import_root: str,
) -> tuple[str, bool]:
    root = Path(str(distribution.locate_file(""))).resolve(strict=True)
    if not root.is_dir():
        raise ValueError
    install_root = _install_root_for(root)
    record_files = [
        file for file in distribution.files or () if str(file).endswith(".dist-info/RECORD")
    ]
    if len(record_files) != 1:
        raise ValueError
    record_parts = _safe_record_parts(str(record_files[0]))
    record_bytes = _read_distribution_file(root, record_parts, install_root=install_root)
    rows = list(csv.reader(io.StringIO(record_bytes.decode("utf-8"), newline="")))
    normalized: list[tuple[str, str, str]] = []
    seen_paths: set[str] = set()
    seen_casefolded_paths: set[str] = set()
    verified_contents: dict[str, bytes] = {}
    for row in rows:
        canonical_path, digest, size, content = _verified_record_row(
            row,
            root=root,
            record_parts=record_parts,
            record_bytes=record_bytes,
            install_root=install_root,
        )
        if canonical_path in seen_paths or canonical_path.casefold() in seen_casefolded_paths:
            raise ValueError
        seen_paths.add(canonical_path)
        seen_casefolded_paths.add(canonical_path.casefold())
        verified_contents[canonical_path] = content
        normalized.append((canonical_path, digest, size))
    record_name = PurePosixPath(*record_parts).as_posix()
    if record_name not in seen_paths:
        raise ValueError
    production_eligible = not _is_editable_install(verified_contents)
    if production_eligible:
        _verify_import_root_completeness(root, import_root, seen_paths)
    canonical = json.dumps(sorted(normalized), ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest(), production_eligible


def _verified_record_row(
    row: list[str],
    *,
    root: Path,
    record_parts: tuple[str, ...],
    record_bytes: bytes,
    install_root: Path,
) -> tuple[str, str, str, bytes]:
    if len(row) != 3:
        raise ValueError
    path, digest, size = row
    parts = _safe_record_parts(path)
    canonical_path = PurePosixPath(*parts).as_posix()
    if parts == record_parts:
        if digest or size:
            raise ValueError
        return canonical_path, digest, size, record_bytes
    if _RECORD_HASH.fullmatch(digest) is None or not size.isdigit():
        raise ValueError
    content = _read_distribution_file(root, parts, install_root=install_root)
    expected = base64.urlsafe_b64decode(f"{digest.removeprefix('sha256=')}=")
    if len(content) != int(size) or not hmac.compare_digest(
        hashlib.sha256(content).digest(), expected
    ):
        raise ValueError
    return canonical_path, digest, size, content


def _safe_record_parts(value: str) -> tuple[str, ...]:
    if not value or "\\" in value or value.startswith("/"):
        raise ValueError
    parts = tuple(value.split("/"))
    if any(not part or part == "." or ":" in part for part in parts):
        raise ValueError
    if PurePosixPath(*parts).is_absolute():
        raise ValueError
    return parts


def _read_distribution_file(
    root: Path,
    parts: tuple[str, ...],
    *,
    install_root: Path,
) -> bytes:
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ValueError
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(install_root) or not resolved.is_file():
        raise ValueError
    return resolved.read_bytes()


def _install_root_for(distribution_root: Path) -> Path:
    environment_root = Path(sys.prefix).resolve(strict=True)
    return (
        environment_root
        if distribution_root.is_relative_to(environment_root)
        else distribution_root
    )


def _verify_import_root_completeness(root: Path, import_root: str, record_paths: set[str]) -> None:
    import_roots: list[Path] = []
    with os.scandir(root) as entries:
        for entry in entries:
            lowered = entry.name.lower()
            is_package_root = lowered == import_root.lower() and (
                entry.is_dir(follow_symlinks=False) or entry.is_symlink()
            )
            is_module_root = (
                (lowered == import_root.lower() or lowered.startswith(f"{import_root.lower()}."))
                and lowered.endswith(_IMPORT_SUFFIXES)
                and (entry.is_file(follow_symlinks=False) or entry.is_symlink())
            )
            if is_package_root or is_module_root:
                import_roots.append(Path(entry.path))
    if len(import_roots) != 1:
        raise ValueError
    _verify_import_path(root, import_roots[0], record_paths)


def _verify_import_path(root: Path, path: Path, record_paths: set[str]) -> None:
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError
    relative = path.relative_to(root).as_posix()
    if path.is_file():
        if relative not in record_paths:
            raise ValueError
        return
    if not path.is_dir() or not any(record.startswith(f"{relative}/") for record in record_paths):
        raise ValueError
    with os.scandir(path) as entries:
        for entry in entries:
            _verify_import_entry(root, entry, record_paths)


def _verify_import_entry(
    root: Path,
    entry: os.DirEntry[str],
    record_paths: set[str],
) -> None:
    child = Path(entry.path)
    relative = child.relative_to(root).as_posix()
    if entry.is_symlink() or not child.resolve(strict=True).is_relative_to(root):
        raise ValueError
    if entry.is_dir(follow_symlinks=False):
        if not any(record.startswith(f"{relative}/") for record in record_paths):
            raise ValueError
        _verify_import_path(root, child, record_paths)
        return
    if not entry.is_file(follow_symlinks=False):
        raise ValueError
    if child.name.lower().endswith(_IMPORT_SUFFIXES) and relative not in record_paths:
        raise ValueError


def _is_editable_install(contents: Mapping[str, bytes]) -> bool:
    for path, content in contents.items():
        filename = PurePosixPath(path).name.lower()
        if filename.endswith(".pth") and "editable" in filename:
            return True
        if not path.endswith(".dist-info/direct_url.json"):
            continue
        document = json.loads(content)
        if isinstance(document, dict):
            directory = document.get("dir_info")
            if isinstance(directory, dict) and directory.get("editable") is True:
                return True
    return False


__all__ = [
    "CANONICAL_RECORD_ATTESTATION",
    "InstalledDistributionAttestation",
    "attest_installed_distribution",
    "attest_installed_resource_distribution",
]
