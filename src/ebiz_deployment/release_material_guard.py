"""Narrow release-material guard for explicitly selected diff and wheel files."""

from __future__ import annotations

import argparse
import re
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import urlsplit

_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bmcp_[a-fA-F0-9]{24,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{16,}\b"),
)
_ENDPOINT_ASSIGNMENT = re.compile(
    r"(?ix)"
    r"(?<![A-Za-z0-9_])[\"']?"
    r"(?:default_)?(?:url|endpoint|base_url|broker_url|mcp_endpoint|openai_endpoint)[\"']?"
    r"(?![A-Za-z0-9_])"
    r"\s*[:=]\s*[\"']?(https?://[^\s\"',}\]]+)"
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_RESERVED_DOCUMENTATION_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".invalid",
    ".test",
)


class ReleaseMaterialError(ValueError):
    """Selected release material is unsafe or cannot be inspected."""


def verify_release_material(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Inspect only explicit files, including text members of explicit wheels.

    This is intentionally not a repository scanner. The release caller supplies
    the files selected from its reviewed diff plus the exact wheels being
    promoted; unrelated operator-local files remain outside the read boundary.
    """

    verified: list[Path] = []
    for candidate in paths:
        path = candidate.resolve()
        if candidate.is_symlink() or not path.is_file():
            raise ReleaseMaterialError(f"release material must be a regular file: {candidate}")
        if path.suffix.lower() == ".whl":
            _inspect_wheel(path)
        else:
            _inspect_bytes(path.read_bytes(), str(path))
        verified.append(path)
    if not verified:
        raise ReleaseMaterialError("at least one explicit release material file is required")
    return tuple(verified)


def _inspect_wheel(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                _inspect_bytes(archive.read(member), f"{path}!{member.filename}")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseMaterialError(f"release wheel cannot be inspected: {path}") from exc


def _inspect_bytes(content: bytes, label: str) -> None:
    # Scan ASCII-compatible strings even when a wheel member contains binary
    # framing.  Ignoring malformed UTF-8 cannot manufacture one of the guarded
    # credential or endpoint shapes, while returning early would let a binary
    # member hide such a value.
    text = content.decode("utf-8", errors="ignore")
    if any(pattern.search(text) is not None for pattern in _CREDENTIAL_PATTERNS):
        raise ReleaseMaterialError(f"credential-shaped value found in {label}")
    for match in _ENDPOINT_ASSIGNMENT.finditer(text):
        host = urlsplit(match.group(1)).hostname
        normalized_host = host.lower() if host is not None else ""
        is_documentation_host = any(
            normalized_host == suffix[1:] or normalized_host.endswith(suffix)
            for suffix in _RESERVED_DOCUMENTATION_SUFFIXES
        )
        if normalized_host not in _LOOPBACK_HOSTS and not is_documentation_host:
            raise ReleaseMaterialError(f"fixed non-loopback runtime endpoint found in {label}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect explicitly selected release diff files and wheels"
    )
    parser.add_argument("paths", type=Path, nargs="+")
    arguments = parser.parse_args(argv)
    verify_release_material(arguments.paths)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the console entry point
    raise SystemExit(main())


__all__ = ["ReleaseMaterialError", "main", "verify_release_material"]
