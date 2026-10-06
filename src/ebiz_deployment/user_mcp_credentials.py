"""Trusted user references and request-scoped, service-specific MCP key lookup."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Literal

import httpx
from base_ai.providers import ResolvedRequestCredential

from .service_discovery import ServiceResolver

_REFERENCE = re.compile(r"mcpuser\.v1\.([0-9a-f]+)\.([0-9a-f]{64})\Z")
_KEY_PATHS = {
    "AGGR": "/erp/feign/mcp/key/query/aggr-key",
    "ERP": "/erp/feign/mcp/key/query/erp-key",
}


class UserMcpRefCodec:
    """Bind a non-secret user reference to a tenant without trusting request payloads."""

    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("MCP user reference signing key is invalid")
        self._key = signing_key

    @property
    def digest(self) -> str:
        return hashlib.sha256(b"mcp-ref-signer-v1\0" + self._key).hexdigest()

    def issue(self, cid: str, uid: str) -> str:
        if not cid or not uid or len(cid) > 256 or len(uid) > 256:
            raise ValueError("MCP user identity is invalid")
        encoded_uid = uid.encode("utf-8").hex()
        signature = self._signature(cid, uid)
        reference = f"mcpuser.v1.{encoded_uid}.{signature}"
        if len(reference) > 256:
            raise ValueError("MCP user identity exceeds the reference limit")
        return reference

    def parse(self, reference: str, *, expected_tenant_id: str) -> str:
        if len(reference) > 256 or not expected_tenant_id:
            raise ValueError("MCP user reference is invalid")
        matched = _REFERENCE.fullmatch(reference)
        if matched is None:
            raise ValueError("MCP user reference is invalid")
        try:
            uid = bytes.fromhex(matched.group(1)).decode("utf-8")
        except (UnicodeError, ValueError):
            raise ValueError("MCP user reference is invalid") from None
        if not uid or not hmac.compare_digest(
            matched.group(2), self._signature(expected_tenant_id, uid)
        ):
            raise ValueError("MCP user reference is invalid")
        return uid

    def _signature(self, cid: str, uid: str) -> str:
        cid_bytes = cid.encode("utf-8")
        uid_bytes = uid.encode("utf-8")
        message = (
            b"mcp-user-v1\0"
            + len(cid_bytes).to_bytes(2, "big")
            + cid_bytes
            + len(uid_bytes).to_bytes(2, "big")
            + uid_bytes
        )
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()


class McpUserKeyResolver:
    """Query exactly one configured key family; never try another user's/family's key."""

    def __init__(
        self,
        *,
        discovery: ServiceResolver,
        client: httpx.AsyncClient,
        codec: UserMcpRefCodec,
        group_name: str = "DEFAULT_GROUP",
        timeout_seconds: float = 5.0,
        service_type: Literal["ERP", "AGGR"] = "AGGR",
        query_token: str | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not group_name:
            raise ValueError("MCP key query settings are invalid")
        if service_type not in _KEY_PATHS or (
            service_type == "ERP"
            and (query_token is None or re.fullmatch(r"[A-Za-z0-9_-]{32,512}", query_token) is None)
        ):
            raise ValueError("MCP key query settings are invalid")
        self._service_type = service_type
        self._query_token = query_token
        self._discovery = discovery
        self._client = client
        self._group = group_name
        self._codec = codec
        self._timeout = timeout_seconds

    async def resolve(
        self, credential_ref: str, *, provider_id: str, expected_tenant_id: str
    ) -> ResolvedRequestCredential:
        if provider_id != "mcp.streamable_http":
            raise ValueError("MCP key unavailable")
        uid = self._codec.parse(credential_ref, expected_tenant_id=expected_tenant_id)
        headers = {"Accept": "application/json"}
        if self._service_type == "ERP":
            headers["Authorization"] = f"Bearer {self._query_token}"
        try:
            origin = await self._discovery.resolve("McpServer", self._group)
            response = await self._client.post(
                origin + _KEY_PATHS[self._service_type],
                json={"cid": expected_tenant_id, "uid": uid},
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise ValueError("MCP key unavailable") from None
        if not isinstance(body, dict) or body.get("success") is not True or body.get("code") != 200:
            raise ValueError("MCP key unavailable")
        result = body.get("result")
        if not isinstance(result, dict):
            raise ValueError("MCP key unavailable")
        # ERP's new contract always includes identity. Legacy AGGR may omit it,
        # but any declared identity must still match the trusted request.
        for name, expected in (("cid", expected_tenant_id), ("uid", uid)):
            if (self._service_type == "ERP" or name in result) and result.get(name) != expected:
                raise ValueError("MCP key unavailable")
        key = result.get("mcpKey")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("MCP key unavailable")
        key_id = result.get("keyId")
        binding = hashlib.sha256(
            f"mcp-{self._service_type.lower()}-key\0{expected_tenant_id}\0{uid}\0{key_id}".encode()
        ).hexdigest()
        return ResolvedRequestCredential(
            secret_value=key,
            tenant_id=expected_tenant_id,
            tenant_binding_digest=binding,
        )


__all__ = ["McpUserKeyResolver", "UserMcpRefCodec"]
