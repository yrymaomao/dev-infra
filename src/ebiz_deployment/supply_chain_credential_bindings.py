"""Deployment-owned, tenant/user to opaque credential-reference authorization map.

The file contains references only, never MCP keys. Both BFF and Tool Gateway
pin the same externally supplied file digest before accepting new work.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 1_048_576


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("credential binding document has duplicate JSON keys")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CredentialBindings:
    digest: str
    _by_owner: dict[tuple[str, str], str] = field(repr=False)

    @classmethod
    def load(cls, path: Path, *, expected_sha256: str) -> CredentialBindings:
        if not path.is_absolute() or not _DIGEST.fullmatch(expected_sha256):
            raise ValueError("credential binding path or digest is invalid")
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError("credential binding document is unavailable") from None
        if not data or len(data) > _MAX_BYTES:
            raise ValueError("credential binding document size is invalid")
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise ValueError("credential binding digest mismatch")
        try:
            document = json.loads(data, object_pairs_hook=_unique_pairs)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("credential binding document is invalid") from error
        if not isinstance(document, dict) or set(document) != {"bindings"}:
            raise ValueError("credential binding document is invalid")
        rows = document["bindings"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 10_000:
            raise ValueError("credential binding count is invalid")
        by_owner: dict[tuple[str, str], str] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"cid", "uid", "credential_ref"}:
                raise ValueError("credential binding entry is invalid")
            cid, uid, reference = row["cid"], row["uid"], row["credential_ref"]
            if (
                not isinstance(cid, str)
                or not _IDENTIFIER.fullmatch(cid)
                or not isinstance(uid, str)
                or not _IDENTIFIER.fullmatch(uid)
                or not isinstance(reference, str)
                or not _REFERENCE.fullmatch(reference)
            ):
                raise ValueError("credential binding entry is invalid")
            owner = (cid, uid)
            if owner in by_owner:
                raise ValueError("duplicate credential binding identity")
            by_owner[owner] = reference
        return cls(digest=digest, _by_owner=by_owner)

    def resolve(self, cid: str, uid: str) -> str:
        try:
            return self._by_owner[cid, uid]
        except KeyError:
            raise LookupError("credential binding is unavailable") from None
