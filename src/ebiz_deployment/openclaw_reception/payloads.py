"""Restricted payload access shared by every BFF repository.

Every prompt, event and analysis a repository persists goes through the
Runtime ``PayloadStore`` under one required permission; the row only keeps the
reference and the hash. Subclasses pick the permission (``supply-chain:level2``
for the Level 2 repository, one per reception profile for conversations), the
mechanics are identical.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from agent_runtime.payloads.contracts import PayloadAuthorizationError, PayloadStore
from pydantic import JsonValue


class RestrictedPayloadAccess:
    def __init__(self, payload_store: PayloadStore, *, permission: str) -> None:
        if not permission:
            raise ValueError("a payload permission is required")
        self._payload_store = payload_store
        self._payload_permission = permission

    async def _stage(self, tenant_id: str, payload: Mapping[str, object]) -> Any:
        staged = await self._payload_store.put_exact_restricted(
            tenant_id=tenant_id,
            payload=cast(JsonValue, dict(payload)),
            required_permission=self._payload_permission,
        )
        if staged.payload_ref is None:
            raise RuntimeError("BFF payloads must use controlled external storage")
        return staged

    async def _commit_staged(self, tenant_id: str, staged: Any) -> None:
        await self._payload_store.ensure_committed(
            tenant_id=tenant_id,
            payload_ref=staged.payload_ref,
            payload_hash=staged.payload_hash,
            size_bytes=staged.size_bytes,
            content_type=staged.content_type,
            classification=staged.classification,
            required_permission=staged.required_permission,
        )

    async def _load(
        self,
        tenant_id: str,
        payload_ref: str | None,
        expected_hash: str | None,
    ) -> dict[str, Any]:
        if payload_ref is None:
            return {}
        try:
            data = await self._payload_store.get_authorized(
                tenant_id=tenant_id,
                payload_ref=payload_ref,
                permission_scope=self._payload_permission,
            )
        except PayloadAuthorizationError:
            staged = await self._payload_store.inspect_for_finalization(
                tenant_id=tenant_id,
                payload_ref=payload_ref,
                permission_scope=self._payload_permission,
            )
            if expected_hash is not None and staged.payload_hash != expected_hash:
                raise ValueError("payload identity mismatch")
            await self._commit_staged(tenant_id, staged)
            data = await self._payload_store.get_authorized(
                tenant_id=tenant_id,
                payload_ref=payload_ref,
                permission_scope=self._payload_permission,
            )
        if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError("payload hash mismatch")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("payload contract is invalid")
        return value


__all__ = ["RestrictedPayloadAccess"]
