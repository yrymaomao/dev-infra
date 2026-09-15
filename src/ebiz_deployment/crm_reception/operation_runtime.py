"""Runtime-owner reads and the approval decision for the workbench operation page.

The BFF process never holds a Gateway tool identity, and v1.5 section 6.3
forbids reusing the Gateway's "same owner / same session" rule for approvers,
so the page is served from the Runtime owner's authenticated APIs instead of
the Gateway status route. Every call carries a short-lived HS256 JWT minted
with the BFF's Runtime secret (``APP_JWT_SECRET``, the same credential path
the dispatcher and the Level 2 worker use), bound to the deployment tenant and
to the *human* the CRM session resolved: ``sub`` is the deterministic actor id
``uuid5("ebizhub:openclaw:principal:<tenant>:<actorId>")`` the Gateway already
records for that person when they start a conversation, ``actor_type`` is
``HUMAN`` and ``roles`` are the CRM session roles. The Runtime's own
``ApprovalService.decide`` therefore records the human as ``decided_by`` and
checks the roles against the approval's ``approver_roles``; no request field
ever names a tenant or actor.

Routes (see ``docs/crm-profile.md`` for what exists and what P2-E still owes):

| seam method        | Runtime route                                             |
| ------------------ | --------------------------------------------------------- |
| ``read_operation`` | ``GET /v1/workbench/operations/{operation_id}``           |
| ``read_execution`` | ``GET /v1/executions/{execution_id}`` (exists)            |
| ``read_approval``  | ``GET /v1/approvals/{approval_id}``                       |
| ``read_settlement``| ``GET /v1/executions/{execution_id}/write-settlement``    |
| ``decide``         | ``POST /v1/approvals/{approval_id}/decision`` (exists)    |
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
import jwt
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from .operation_contracts import OperationStatusV2, parse_operation_status

READ_SCOPES = ("workflow:read",)
DECIDE_SCOPES = ("workflow:read", "approval:decide")
ACTOR_TYPE = "HUMAN"
TOKEN_TTL_SECONDS = 60
WORKBENCH_OPERATION_PATH = "/v1/workbench/operations/{operation_id}"
EXECUTION_PATH = "/v1/executions/{execution_id}"
APPROVAL_PATH = "/v1/approvals/{approval_id}"
APPROVAL_DECISION_PATH = "/v1/approvals/{approval_id}/decision"
WRITE_SETTLEMENT_PATH = "/v1/executions/{execution_id}/write-settlement"

DecisionRefusal = Literal["conflict", "expired", "forbidden", "hidden"]


@dataclass(frozen=True, slots=True)
class WorkbenchActor:
    """The human the CRM session resolved; never taken from the request body."""

    tenant_id: str
    actor_id: str
    roles: tuple[str, ...]

    @property
    def runtime_actor_id(self) -> str:
        return str(
            uuid5(NAMESPACE_URL, f"ebizhub:openclaw:principal:{self.tenant_id}:{self.actor_id}")
        )


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """One operation as the owner projects it, plus the facts the profile fence needs."""

    status: OperationStatusV2
    document: dict[str, Any]
    offer_id: str
    session_key: str


@dataclass(frozen=True, slots=True)
class ApprovalPreview:
    """The frozen preview output the approval is bound to (raw, whitelisted later)."""

    case_id: str | None
    case_version: int | None
    proposal: dict[str, Any] | None
    draft: dict[str, Any] | None
    evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ApprovalRecordView:
    approval_id: str
    execution_id: str
    status: str
    row_version: int
    node_id: str | None
    capability_ref: str | None
    input_hash: str
    preview_hash: str | None
    expires_at: datetime
    approval_usable_until: datetime | None
    approver_roles: tuple[str, ...]
    preview: ApprovalPreview | None


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """The public execution snapshot: which approval (if any) it is waiting on."""

    execution_id: str
    status: str
    waiting_approval_id: str | None
    waiting_row_version: int | None
    outputs: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class EffectRecord:
    node_key: str
    status: str
    result_ref: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    effects: tuple[EffectRecord, ...]


class RuntimeDecisionRefused(Exception):
    """The owner refused the decision; ``kind`` is already mapped to the workbench meaning."""

    def __init__(self, kind: DecisionRefusal) -> None:
        super().__init__(kind)
        self.kind: DecisionRefusal = kind


class RuntimeUnavailable(Exception):
    """The Runtime did not give a trustworthy answer (transport, 5xx, malformed body)."""


class OperationRuntime(Protocol):
    async def read_operation(
        self, actor: WorkbenchActor, operation_id: str
    ) -> OperationRecord | None: ...

    async def read_execution(
        self, actor: WorkbenchActor, execution_id: str
    ) -> ExecutionRecord | None: ...

    async def read_approval(
        self, actor: WorkbenchActor, approval_id: str
    ) -> ApprovalRecordView | None: ...

    async def read_settlement(
        self, actor: WorkbenchActor, execution_id: str
    ) -> SettlementRecord | None: ...

    async def decide(
        self,
        actor: WorkbenchActor,
        *,
        approval_id: str,
        row_version: int,
        decision: Literal["APPROVED", "REJECTED"],
        reason: str | None,
    ) -> None: ...


# --- Runtime reply shapes (input validation only; unknown fields ignored) ------

_Text = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class _WorkbenchOperationReply(_Reply):
    status: dict[str, Any]
    offer_id: _Text
    session_key: _Text


class _PreviewReply(_Reply):
    case_id: _Text | None = None
    case_version: int | None = Field(default=None, ge=0)
    proposal: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)


class _ApprovalReply(_Reply):
    approval_id: _Text
    execution_id: _Text
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED", "CONSUMED"]
    row_version: int = Field(ge=0)
    node_id: _Text | None = None
    capability_ref: _Text | None = None
    input_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    preview_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None
    expires_at: AwareDatetime
    approval_usable_until: AwareDatetime | None = None
    approver_roles: list[_Text] = Field(default_factory=list)
    preview: _PreviewReply | None = None


class _WaitingReply(_Reply):
    approval_id: _Text | None = None
    approval_row_version: int | None = Field(default=None, ge=0)


class _ExecutionReply(_Reply):
    execution_id: _Text
    status: _Text
    waiting: _WaitingReply | None = None
    outputs: dict[str, Any] | None = None


class _EffectReply(_Reply):
    node_key: _Text
    status: _Text
    result_ref: _Text | None = None
    observed_at: AwareDatetime


class _SettlementReply(_Reply):
    effects: list[_EffectReply] = Field(default_factory=list)


class HttpOperationRuntime:
    """The real seam: one Runtime origin, one secret, one short-lived token per call."""

    def __init__(
        self,
        *,
        base_url: str,
        secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query:
            raise ValueError("Runtime URL must be an HTTP(S) origin")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Runtime URL requires HTTPS outside loopback")
        if len(secret) < 32:
            raise ValueError("APP_JWT_SECRET must contain at least 32 characters")
        self._base_url = base_url.rstrip("/")
        self._secret = secret
        self._transport = transport
        self._timeout = timeout_seconds

    def _authorization(self, actor: WorkbenchActor, scopes: tuple[str, ...]) -> str:
        issued_at = int(time.time())
        token = jwt.encode(
            {
                "aud": "agent-runtime",
                "sub": actor.runtime_actor_id,
                "tenant_id": actor.tenant_id,
                "actor_type": ACTOR_TYPE,
                "scopes": list(scopes),
                "roles": list(actor.roles),
                "iat": issued_at,
                "exp": issued_at + TOKEN_TTL_SECONDS,
            },
            self._secret,
            algorithm="HS256",
        )
        return f"Bearer {token}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        actor: WorkbenchActor,
        scopes: tuple[str, ...],
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=False,
            ) as client:
                return await client.request(
                    method,
                    self._base_url + path,
                    headers={
                        "Authorization": self._authorization(actor, scopes),
                        "Accept": "application/json",
                    },
                    json=json_body,
                )
        except httpx.HTTPError:
            raise RuntimeUnavailable() from None

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError:
            raise RuntimeUnavailable() from None
        if not isinstance(value, dict):
            raise RuntimeUnavailable()
        return value

    async def read_operation(
        self, actor: WorkbenchActor, operation_id: str
    ) -> OperationRecord | None:
        response = await self._request(
            "GET",
            WORKBENCH_OPERATION_PATH.format(operation_id=operation_id),
            actor=actor,
            scopes=READ_SCOPES,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeUnavailable()
        try:
            reply = _WorkbenchOperationReply.model_validate_json(response.content)
            status, document = parse_operation_status(reply.status)
        except ValueError:
            raise RuntimeUnavailable() from None
        if status.operation_id != operation_id:
            raise RuntimeUnavailable()
        return OperationRecord(
            status=status,
            document=document,
            offer_id=reply.offer_id,
            session_key=reply.session_key,
        )

    async def read_execution(
        self, actor: WorkbenchActor, execution_id: str
    ) -> ExecutionRecord | None:
        response = await self._request(
            "GET",
            EXECUTION_PATH.format(execution_id=execution_id),
            actor=actor,
            scopes=READ_SCOPES,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeUnavailable()
        try:
            reply = _ExecutionReply.model_validate_json(response.content)
        except ValueError:
            raise RuntimeUnavailable() from None
        if reply.execution_id != execution_id:
            raise RuntimeUnavailable()
        waiting = reply.waiting
        return ExecutionRecord(
            execution_id=reply.execution_id,
            status=reply.status,
            waiting_approval_id=waiting.approval_id if waiting is not None else None,
            waiting_row_version=waiting.approval_row_version if waiting is not None else None,
            outputs=reply.outputs,
        )

    async def read_approval(
        self, actor: WorkbenchActor, approval_id: str
    ) -> ApprovalRecordView | None:
        response = await self._request(
            "GET", APPROVAL_PATH.format(approval_id=approval_id), actor=actor, scopes=READ_SCOPES
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeUnavailable()
        try:
            reply = _ApprovalReply.model_validate_json(response.content)
        except ValueError:
            raise RuntimeUnavailable() from None
        if reply.approval_id != approval_id:
            raise RuntimeUnavailable()
        preview = (
            ApprovalPreview(
                case_id=reply.preview.case_id,
                case_version=reply.preview.case_version,
                proposal=reply.preview.proposal,
                draft=reply.preview.draft,
                evidence=tuple(reply.preview.evidence_refs),
            )
            if reply.preview is not None
            else None
        )
        return ApprovalRecordView(
            approval_id=reply.approval_id,
            execution_id=reply.execution_id,
            status=reply.status,
            row_version=reply.row_version,
            node_id=reply.node_id,
            capability_ref=reply.capability_ref,
            input_hash=reply.input_hash,
            preview_hash=reply.preview_hash,
            expires_at=reply.expires_at,
            approval_usable_until=reply.approval_usable_until,
            approver_roles=tuple(reply.approver_roles),
            preview=preview,
        )

    async def read_settlement(
        self, actor: WorkbenchActor, execution_id: str
    ) -> SettlementRecord | None:
        response = await self._request(
            "GET",
            WRITE_SETTLEMENT_PATH.format(execution_id=execution_id),
            actor=actor,
            scopes=READ_SCOPES,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeUnavailable()
        try:
            reply = _SettlementReply.model_validate_json(response.content)
        except ValueError:
            raise RuntimeUnavailable() from None
        return SettlementRecord(
            effects=tuple(
                EffectRecord(
                    node_key=item.node_key,
                    status=item.status,
                    result_ref=item.result_ref,
                    observed_at=item.observed_at,
                )
                for item in reply.effects
            )
        )

    async def decide(
        self,
        actor: WorkbenchActor,
        *,
        approval_id: str,
        row_version: int,
        decision: Literal["APPROVED", "REJECTED"],
        reason: str | None,
    ) -> None:
        response = await self._request(
            "POST",
            APPROVAL_DECISION_PATH.format(approval_id=approval_id),
            actor=actor,
            scopes=DECIDE_SCOPES,
            json_body={
                "outcome": decision,
                "reason": reason,
                "expected_row_version": row_version,
            },
        )
        if response.status_code == 200:
            return
        code = str(self._body(response).get("error_code", "")) if response.content else ""
        if response.status_code == 404:
            raise RuntimeDecisionRefused("hidden")
        if code == "APPROVAL_EXPIRED":
            raise RuntimeDecisionRefused("expired")
        if response.status_code == 403 or code == "APPROVAL_ROLE_DENIED":
            raise RuntimeDecisionRefused("forbidden")
        if response.status_code == 409:
            raise RuntimeDecisionRefused("conflict")
        raise RuntimeUnavailable()


__all__ = [
    "ACTOR_TYPE",
    "APPROVAL_DECISION_PATH",
    "APPROVAL_PATH",
    "DECIDE_SCOPES",
    "EXECUTION_PATH",
    "READ_SCOPES",
    "TOKEN_TTL_SECONDS",
    "WORKBENCH_OPERATION_PATH",
    "WRITE_SETTLEMENT_PATH",
    "ApprovalPreview",
    "ApprovalRecordView",
    "EffectRecord",
    "ExecutionRecord",
    "HttpOperationRuntime",
    "OperationRecord",
    "OperationRuntime",
    "RuntimeDecisionRefused",
    "RuntimeUnavailable",
    "SettlementRecord",
    "WorkbenchActor",
]
