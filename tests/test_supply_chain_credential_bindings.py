from __future__ import annotations

import hashlib

import pytest

from ebiz_deployment.supply_chain_credential_bindings import CredentialBindings


def test_exact_user_binding_and_digest(tmp_path) -> None:
    document = (
        b'{"bindings":[{"cid":"tenant-a","uid":"alice","credential_ref":"key:alice"},'
        b'{"cid":"tenant-a","uid":"bob","credential_ref":"key:bob"}]}'
    )
    path = tmp_path / "bindings.json"
    path.write_bytes(document)
    bindings = CredentialBindings.load(path, expected_sha256=hashlib.sha256(document).hexdigest())

    assert bindings.resolve("tenant-a", "alice") == "key:alice"
    assert bindings.resolve("tenant-a", "bob") == "key:bob"
    with pytest.raises(LookupError):
        bindings.resolve("tenant-b", "alice")
    with pytest.raises(LookupError):
        bindings.resolve("tenant-a", "charlie")


def test_binding_rejects_duplicate_identity_and_digest_drift(tmp_path) -> None:
    document = (
        b'{"bindings":[{"cid":"tenant-a","uid":"alice","credential_ref":"key:first"},'
        b'{"cid":"tenant-a","uid":"alice","credential_ref":"key:second"}]}'
    )
    path = tmp_path / "bindings.json"
    path.write_bytes(document)
    digest = hashlib.sha256(document).hexdigest()
    with pytest.raises(ValueError, match="duplicate"):
        CredentialBindings.load(path, expected_sha256=digest)
    with pytest.raises(ValueError, match="digest"):
        CredentialBindings.load(path, expected_sha256="0" * 64)
