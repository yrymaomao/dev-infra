"""Trusted user references and request-scoped, service-specific MCP key lookup."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from typing import Literal

import httpx
from base_ai.providers import ResolvedRequestCredential

from .erp_session import SessionInvalid, SessionUnavailable, UserLoginTokenSource
from .service_discovery import ServiceResolver

_REFERENCE = re.compile(r"mcpuser\.v1\.([0-9a-f]+)\.([0-9a-f]{64})\Z")
_AGGR_KEY_PATH = "/erp/feign/mcp/key/query/aggr-key"


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
        login_session: UserLoginTokenSource | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 30 or not group_name:
            raise ValueError("MCP key query settings are invalid")
        if service_type not in {"ERP", "AGGR"} or (service_type == "ERP" and login_session is None):
            raise ValueError("MCP key query settings are invalid")
        self._service_type = service_type
        self._login_session = login_session
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
        try:
            async with asyncio.timeout(self._timeout):
                if self._service_type == "ERP":
                    result = await self._erp_key(uid, expected_tenant_id)
                    key = result.get("key")
                else:
                    origin = await self._discovery.resolve("McpServer", self._group)
                    result = await self._post_result(
                        origin + _AGGR_KEY_PATH,
                        {"cid": expected_tenant_id, "uid": uid},
                        {"Accept": "application/json"},
                    )
                    key = result.get("mcpKey")
                # Existing APIs may omit identity, but declared identity must match.
                for name, expected in (("cid", expected_tenant_id), ("uid", uid)):
                    if name in result and result[name] != expected:
                        raise ValueError("MCP key unavailable")
        except (httpx.HTTPError, ValueError, TimeoutError, SessionInvalid, SessionUnavailable):
            raise ValueError("MCP key unavailable") from None
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 4096
            or not key.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in key)
        ):
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

    async def _post_result(
        self, url: str, payload: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        response = await self._client.post(
            url, json=payload, headers=headers, timeout=self._timeout, follow_redirects=False
        )
        response.raise_for_status()
        if len(response.content) > 1024 * 1024:
            raise ValueError("MCP key unavailable")
        body = response.json()
        if not isinstance(body, dict) or body.get("success") is not True or body.get("code") != 200:
            raise ValueError("MCP key unavailable")
        result = body.get("result")
        if not isinstance(result, dict):
            raise ValueError("MCP key unavailable")
        return result

    async def _erp_key(self, uid: str, cid: str) -> dict[str, object]:
        if self._login_session is None:
            raise ValueError("MCP key unavailable")
        token = await self._login_session.current_token(uid, expected_tenant_id=cid)
        headers = {"Accept": "application/json", "_token_": token, "timezone": "0"}
        origin = await self._discovery.resolve("McpServer", self._group)
        seen: set[int] = set()
        # Server defaults to newest creation first. Bound scans; never choose
        # tenant-list or a caller-supplied key ID, nor fall back after a failure.
        for page in range(1, 11):
            listing = await self._post_result(
                origin + "/erp/mcp-key/list",
                {"enabled": True, "pageIndex": page, "pageSize": 100},
                headers,
            )
            records, total = listing.get("records"), listing.get("total")
            if (
                not isinstance(records, list)
                or len(records) > 100
                or type(total) is not int
                or total < 0
                or len(records) > total
                or not records
            ):
                raise ValueError("MCP key unavailable")
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("MCP key unavailable")
                key_id = record.get("keyId")
                if type(key_id) is not int or not 0 < key_id <= 2**63 - 1 or key_id in seen:
                    raise ValueError("MCP key unavailable")
                seen.add(key_id)
                if record.get("serviceType") != "ERP" or record.get("enabled") is not True:
                    continue
                result = await self._post_result(
                    origin + "/erp/mcp-key/detail",
                    {"keyId": key_id},
                    headers,
                )
                if (
                    type(result.get("keyId")) is not int
                    or result.get("keyId") != key_id
                    or result.get("serviceType") != "ERP"
                    or type(result.get("enabled")) is not int
                    or result.get("enabled") != 1
                ):
                    raise ValueError("MCP key unavailable")
                return result
            if page * 100 >= total:
                break
        raise ValueError("MCP key unavailable")


__all__ = ["McpUserKeyResolver", "UserMcpRefCodec"]
