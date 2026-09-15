"""Contracts of the workbench operation routes (OpenClaw x CRM Phase 2, P2-G).

Two halves:

* a strict BFF-side mirror of the Tool Gateway wire v2 ``OperationStatusV2``
  (Runtime P2-A, ``ebiz_runtime_contracts.tool_gateway_catalog_v2``). The
  pinned ``ebiz-runtime-contracts`` 0.1.6 wheel predates wire v2, so the BFF
  cannot import the owner model; this mirror keeps the same field set,
  bounds, settlement pairs and projection rules, and
  ``tests/test_crm_operation_contracts.py`` runs it against byte copies of the
  P2-A ``operation_status`` accept/reject fixtures. The route forwards the
  Runtime's status document unchanged once it passed this mirror - the BFF
  never rewrites a state, revision or path.
* the workbench-only reply and request shapes agreed with the emf-crm
  operation page (``src/api/openclawOperations.ts`` on
  ``codex/emf-crm-operation-page``).
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, Self

from ebiz_runtime_contracts import RuntimeErrorDetail
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

# --- wire v2 aliases (same bounds as the owner module) ------------------------

WireVersionV2 = Literal["2"]
GatewayId = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
MAX_REVISION_V2 = 2**53 - 1
Revision = Annotated[int, Field(ge=0, le=MAX_REVISION_V2)]
RetryAfterSeconds = Annotated[int, Field(ge=1, le=3600)]
MAX_RESULT_BYTES = 1_048_576
#: Server-fixed relative path: absolute-path form, safe segment characters only,
#: no scheme, authority, query, fragment or dot-led segment.
INTERACTION_PATH_PATTERN = r"^(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)+$"
InteractionPath = Annotated[
    str, StringConstraints(min_length=2, max_length=512, pattern=INTERACTION_PATH_PATTERN)
]
CatalogText = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")
]

OperationState = Literal[
    "pending",
    "running",
    "waiting_approval",
    "waiting_input",
    "waiting_external",
    "reconciling",
    "completed",
    "error",
    "cancelled",
]
OperationNextAction = Literal["poll_status", "open_ebizhub", "read_result", "none"]
SettlementState = Literal["not_started", "pending", "resolved"]
SettlementEffect = Literal["none", "unknown", "confirmed", "partial"]
InteractionKind = Literal["approval", "input", "review"]

OPERATION_NEXT_ACTIONS_V2: dict[str, tuple[str, ...]] = {
    "pending": ("poll_status",),
    "running": ("poll_status",),
    "waiting_approval": ("open_ebizhub",),
    "waiting_input": ("open_ebizhub",),
    "waiting_external": ("poll_status",),
    "reconciling": ("poll_status", "open_ebizhub"),
    "completed": ("read_result",),
    "error": ("none",),
    "cancelled": ("none",),
}
TERMINAL_OPERATION_STATES_V2 = frozenset({"completed", "error", "cancelled"})
SETTLEMENT_PAIRS_V2 = frozenset(
    {
        ("not_started", "none"),
        ("pending", "unknown"),
        ("resolved", "none"),
        ("resolved", "confirmed"),
        ("resolved", "partial"),
    }
)
_POLLING_ACTIONS = frozenset({"poll_status", "open_ebizhub"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SettlementV2(_Strict):
    state: SettlementState
    effect: SettlementEffect

    @model_validator(mode="after")
    def legal_pair(self) -> Self:
        if (self.state, self.effect) not in SETTLEMENT_PAIRS_V2:
            raise ValueError(f"settlement {self.state}/{self.effect} is not a legal pair")
        return self


class InteractionV2(_Strict):
    kind: InteractionKind
    revision: Revision
    path: InteractionPath
    label: CatalogText


class ResultReadV2(_Strict):
    schema_version: WireVersionV2
    whole_digest: Sha256
    total_size_bytes: int = Field(ge=1, le=MAX_RESULT_BYTES)
    next_offset: int = Field(default=0, ge=0, lt=MAX_RESULT_BYTES)


class OperationStatusV2(_Strict):
    """Mirror of the owner's value-free operation projection (wire v2)."""

    schema_version: WireVersionV2
    operation_id: GatewayId
    state: OperationState
    revision: Revision
    observed_at: AwareDatetime
    execution_id: GatewayId | None = None
    execution_deadline_at: AwareDatetime | None = None
    cancel_requested: bool
    settlement: SettlementV2
    next_action: OperationNextAction
    retry_after_seconds: RetryAfterSeconds | None = None
    interaction: InteractionV2 | None = None
    result_read: ResultReadV2 | None = None
    error: RuntimeErrorDetail | None = None

    @model_validator(mode="after")
    def consistent_projection(self) -> Self:
        if self.state in TERMINAL_OPERATION_STATES_V2 and self.settlement.state != "resolved":
            raise ValueError("a terminal operation projection requires resolved settlement")
        if self.next_action not in OPERATION_NEXT_ACTIONS_V2[self.state]:
            raise ValueError(f"next action {self.next_action} is not allowed in {self.state}")
        if (self.retry_after_seconds is not None) != (self.next_action in _POLLING_ACTIONS):
            raise ValueError("retry timing is supplied exactly for polling and waiting guidance")
        if (self.interaction is not None) != (self.next_action == "open_ebizhub"):
            raise ValueError("an interaction is supplied exactly for the open_ebizhub action")
        if self.result_read is not None and self.next_action != "read_result":
            raise ValueError("controlled result guidance requires the read_result action")
        if (self.error is not None) != (self.state == "error"):
            raise ValueError("only an error operation carries sanitized error detail")
        return self


# --- workbench detail (BFF -> emf-crm) ----------------------------------------

OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
#: ``audit_href`` may carry a query; ``interaction.path`` may not (wire rule above).
AUDIT_HREF_PATTERN = re.compile(
    r"^/(?!/)[A-Za-z0-9_~-][A-Za-z0-9._~\-/]*(?:\?[A-Za-z0-9._~\-=&%]*)?$"
)
DisplayText = Annotated[str, StringConstraints(max_length=2048)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
JournalState = Literal[
    "PENDING",
    "APPLIED_UNVERIFIED",
    "UNKNOWN",
    "CONFIRMED",
    "VERIFY_FAILED",
    "FAILED",
    "COMPENSATED",
]


def safe_audit_href(value: str) -> str:
    if AUDIT_HREF_PATTERN.fullmatch(value) is None or "/.." in value or "/./" in value:
        raise ValueError("audit_href must be a server-fixed relative path")
    return value


class OperationCaseRef(_Strict):
    case_id: ShortText
    case_version: int = Field(ge=0)


class EvidenceItem(_Strict):
    source_type: ShortText
    ref: ShortText
    summary: DisplayText


class ApprovalProposal(_Strict):
    action_type: ShortText
    impact_summary: DisplayText
    risk_summary: DisplayText
    target_thread_ref: ShortText
    body_hash: Sha256


class ApprovalDraft(_Strict):
    body: Annotated[str, StringConstraints(max_length=32768)]
    language: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class OperationApproval(_Strict):
    approval_id: GatewayId
    row_version: int = Field(ge=0)
    node_ref: ShortText
    capability_ref: ShortText
    input_hash: Sha256
    preview_hash: Sha256
    proposal: ApprovalProposal
    draft: ApprovalDraft | None
    deadline_at: AwareDatetime
    decidable: bool


class OperationEffect(_Strict):
    node_ref: ShortText
    journal_state: JournalState
    receipt_ref: ShortText | None
    observed_at: AwareDatetime


class OperationDetail(BaseModel):
    """The GET reply. ``status`` is the Runtime document as received (already mirrored)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: dict[str, Any]
    case: OperationCaseRef | None
    evidence: tuple[EvidenceItem, ...]
    approval: OperationApproval | None
    effects: tuple[OperationEffect, ...]
    audit_href: str | None

    @model_validator(mode="after")
    def links_are_server_fixed(self) -> Self:
        if self.audit_href is not None:
            safe_audit_href(self.audit_href)
        return self


class ApprovalDecisionRequest(_Strict):
    approval_id: GatewayId
    row_version: int = Field(ge=0)
    decision: Literal["APPROVED", "REJECTED"]
    reason: Annotated[str, StringConstraints(max_length=2048)]


def parse_operation_status(document: object) -> tuple[OperationStatusV2, dict[str, Any]]:
    """Validate one Runtime status document; return the model and the untouched dict."""

    if not isinstance(document, dict):
        raise ValueError("operation status must be a JSON object")
    # JSON-mode strict validation: RFC3339 strings become aware datetimes, every
    # other field keeps its exact JSON type (no "8" -> 8, no "false" -> False).
    status = OperationStatusV2.model_validate_json(json.dumps(document))
    return status, dict(document)


__all__ = [
    "AUDIT_HREF_PATTERN",
    "INTERACTION_PATH_PATTERN",
    "OPERATION_ID_PATTERN",
    "OPERATION_NEXT_ACTIONS_V2",
    "SETTLEMENT_PAIRS_V2",
    "TERMINAL_OPERATION_STATES_V2",
    "ApprovalDecisionRequest",
    "ApprovalDraft",
    "ApprovalProposal",
    "EvidenceItem",
    "InteractionV2",
    "JournalState",
    "OperationApproval",
    "OperationCaseRef",
    "OperationDetail",
    "OperationEffect",
    "OperationStatusV2",
    "ResultReadV2",
    "SettlementV2",
    "parse_operation_status",
    "safe_audit_href",
]
