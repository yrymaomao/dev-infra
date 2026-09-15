"""Workbench operation routes of the CRM profile (OpenClaw x CRM Phase 2, P2-G).

``GET  {api_prefix}/operations/{operation_id}`` and
``POST {api_prefix}/operations/{operation_id}/approval/decision`` serve the
emf-crm operation page (v1.5 sections 6.3, 8.3, 12.2; integrated design 12.3).

Order of every request, and what each step may touch:

1. operation id syntax (no call);
2. the workbench ``_token_`` through crm-service ``session-principal`` (no
   Runtime call); a missing, invalid or machine session is hidden;
3. the tenant fence: the session's tenant must be this deployment's tenant
   (no Runtime call before this passes);
4. the Runtime owner read, tenant-bound by the token; the operation must then
   belong to this profile (offer id and session-key prefix) or it is hidden;
5. only now approval, execution and settlement facts are read and projected.

Unknown, foreign-tenant, other-profile and unauthenticated requests all
answer the identical ``404`` envelope (A02): nothing distinguishes them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Cookie, Header, Request
from fastapi.responses import JSONResponse

from ebiz_deployment.openclaw_reception.profile import ReceptionProfile

from .operation_contracts import (
    OPERATION_ID_PATTERN,
    ApprovalDecisionRequest,
    ApprovalDraft,
    ApprovalProposal,
    EvidenceItem,
    OperationApproval,
    OperationCaseRef,
    OperationDetail,
    OperationEffect,
    safe_audit_href,
)
from .operation_runtime import (
    ApprovalRecordView,
    OperationRecord,
    OperationRuntime,
    RuntimeDecisionRefused,
    RuntimeUnavailable,
    WorkbenchActor,
)
from .session_api import (
    TOKEN_NAME,
    SessionInvalid,
    SessionPrincipalClient,
    SessionUnavailable,
)

AUDIT_PATH = "/crm/audit"
_MAX_TOKEN = 4096
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_EVIDENCE_REF_KEYS = ("evidence_id", "ref")
_DRAFT_LANGUAGE_KEYS = ("language", "locale")
_JOURNAL_STATES = frozenset(
    {
        "PENDING",
        "APPLIED_UNVERIFIED",
        "UNKNOWN",
        "CONFIRMED",
        "VERIFY_FAILED",
        "FAILED",
        "COMPENSATED",
    }
)

Clock = Callable[[], datetime]


def _request_id(request: Request) -> str | None:
    value = request.headers.get("x-request-id")
    return value if value is not None and _REQUEST_ID.fullmatch(value) else None


def _error(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    *,
    category: str,
    phase: str = "recovery",
    retryable: bool = False,
) -> JSONResponse:
    """The standard CRM/BFF error envelope (same key set as ``app._safe_error``)."""

    return JSONResponse(
        status_code=status_code,
        content={
            "error_code": error_code,
            "phase": phase,
            "category": category,
            "retryable": retryable,
            "safe_message": message,
            "request_id": _request_id(request),
        },
    )


def hidden(request: Request) -> JSONResponse:
    """A02: one body for unknown, foreign-tenant, other-profile and unauthenticated."""

    return _error(
        request,
        404,
        "CRM_OPERATION_NOT_FOUND",
        "The operation was not found or is not accessible.",
        category="authorization",
    )


def _unavailable(request: Request, what: str) -> JSONResponse:
    return _error(
        request,
        503,
        f"CRM_{what}_UNAVAILABLE",
        "The operation service is temporarily unavailable.",
        category="transient",
        retryable=True,
    )


# --- projection (pure) ---------------------------------------------------------


def _text(value: object, *, limit: int) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= limit:
        return value
    return None


def project_evidence(items: tuple[dict[str, Any], ...]) -> tuple[EvidenceItem, ...]:
    """Whitelist: ``source_type``, an opaque ``ref`` and an agent-written ``summary`` only.

    Never a content reference, a contact detail or a message body: keys other
    than the three named ones do not exist for the reply, whatever the upstream
    payload carried.
    """

    projected: list[EvidenceItem] = []
    for item in items:
        source_type = _text(item.get("source_type"), limit=256)
        ref = next(
            (found for key in _EVIDENCE_REF_KEYS if (found := _text(item.get(key), limit=256))),
            None,
        )
        if source_type is None or ref is None:
            continue
        summary = _text(item.get("summary"), limit=2048) or ""
        projected.append(EvidenceItem(source_type=source_type, ref=ref, summary=summary))
    return tuple(projected)


def _project_proposal(value: dict[str, Any] | None) -> ApprovalProposal | None:
    if value is None:
        return None
    try:
        return ApprovalProposal(
            action_type=value["action_type"],
            impact_summary=value["impact_summary"],
            risk_summary=value["risk_summary"],
            target_thread_ref=value["target_thread_ref"],
            body_hash=value["body_hash"],
        )
    except (KeyError, ValueError, TypeError):
        return None


def _project_draft(value: dict[str, Any] | None) -> ApprovalDraft | None:
    if value is None:
        return None
    language = next(
        (found for key in _DRAFT_LANGUAGE_KEYS if (found := _text(value.get(key), limit=32))),
        None,
    )
    body = value.get("body")
    if not isinstance(body, str) or language is None:
        return None
    try:
        return ApprovalDraft(body=body, language=language)
    except ValueError:
        return None


def approval_deadline(view: ApprovalRecordView) -> datetime:
    """The 'decide before' instant: the earliest of the usable-until window and the TTL."""

    candidates = [view.expires_at]
    if view.approval_usable_until is not None:
        candidates.append(view.approval_usable_until)
    return min(candidates)


def is_decidable(
    view: ApprovalRecordView, record: OperationRecord, actor: WorkbenchActor, *, now: datetime
) -> bool:
    return (
        view.status == "PENDING"
        and record.status.state == "waiting_approval"
        and now < approval_deadline(view)
        and bool(set(view.approver_roles) & set(actor.roles))
    )


def project_approval(
    view: ApprovalRecordView, record: OperationRecord, actor: WorkbenchActor, *, now: datetime
) -> OperationApproval | None:
    """The approval as the page needs it; ``None`` when the owner's facts are incomplete.

    A proposal and both hashes are required: the page must never show an
    approval it cannot bind to an exact preview.
    """

    preview = view.preview
    proposal = _project_proposal(preview.proposal if preview is not None else None)
    if (
        proposal is None
        or view.preview_hash is None
        or view.node_id is None
        or view.capability_ref is None
    ):
        return None
    return OperationApproval(
        approval_id=view.approval_id,
        row_version=view.row_version,
        node_ref=view.node_id,
        capability_ref=view.capability_ref,
        input_hash=view.input_hash,
        preview_hash=view.preview_hash,
        proposal=proposal,
        draft=_project_draft(preview.draft if preview is not None else None),
        deadline_at=approval_deadline(view),
        decidable=is_decidable(view, record, actor, now=now),
    )


def _project_case(
    view: ApprovalRecordView | None, outputs: dict[str, Any] | None
) -> OperationCaseRef | None:
    candidates: list[tuple[object, object]] = []
    if view is not None and view.preview is not None:
        candidates.append((view.preview.case_id, view.preview.case_version))
    if outputs is not None:
        candidates.append((outputs.get("case_id"), outputs.get("case_version")))
    for case_id, case_version in candidates:
        if (
            isinstance(case_id, str)
            and isinstance(case_version, int)
            and not isinstance(case_version, bool)
        ):
            try:
                return OperationCaseRef(case_id=case_id, case_version=case_version)
            except ValueError:
                continue
    return None


async def build_detail(
    runtime: OperationRuntime,
    actor: WorkbenchActor,
    record: OperationRecord,
    *,
    now: datetime,
) -> OperationDetail:
    """Compose the reply from owner facts; only called after the fence passed."""

    execution_id = record.status.execution_id
    approval_view: ApprovalRecordView | None = None
    outputs: dict[str, Any] | None = None
    effects: list[OperationEffect] = []
    if execution_id is not None:
        execution = await runtime.read_execution(actor, execution_id)
        if execution is not None:
            outputs = execution.outputs
            if execution.waiting_approval_id is not None:
                approval_view = await runtime.read_approval(actor, execution.waiting_approval_id)
                if approval_view is not None and approval_view.execution_id != execution_id:
                    approval_view = None
        settlement = await runtime.read_settlement(actor, execution_id)
        if settlement is not None:
            for effect in settlement.effects:
                if effect.status not in _JOURNAL_STATES:
                    continue
                effects.append(
                    OperationEffect(
                        node_ref=effect.node_key,
                        journal_state=effect.status,  # type: ignore[arg-type]
                        receipt_ref=effect.result_ref,
                        observed_at=effect.observed_at,
                    )
                )
    approval = (
        project_approval(approval_view, record, actor, now=now)
        if approval_view is not None
        else None
    )
    evidence = (
        project_evidence(approval_view.preview.evidence)
        if approval_view is not None and approval_view.preview is not None
        else ()
    )
    audit_href = (
        safe_audit_href(f"{AUDIT_PATH}?execution={execution_id}")
        if execution_id is not None and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", execution_id)
        else None
    )
    return OperationDetail(
        status=record.document,
        case=_project_case(approval_view, outputs),
        evidence=evidence,
        approval=approval,
        effects=tuple(effects),
        audit_href=audit_href,
    )


# --- routes ---------------------------------------------------------------------


def operation_router(
    *,
    profile: ReceptionProfile,
    tenant_id: str,
    session_client: SessionPrincipalClient,
    runtime: OperationRuntime,
    clock: Clock | None = None,
) -> APIRouter:
    router = APIRouter(prefix=profile.api_prefix)
    now = clock or (lambda: datetime.now(UTC))

    async def resolve_actor(
        header_token: str | None, cookie_token: str | None
    ) -> WorkbenchActor | None:
        token = (header_token or cookie_token or "").strip()
        if not token or len(token) > _MAX_TOKEN:
            return None
        try:
            principal = await session_client.resolve(token)
        except SessionInvalid:
            return None
        if principal.tenant_id != tenant_id:
            return None
        return WorkbenchActor(
            tenant_id=principal.tenant_id, actor_id=principal.actor_id, roles=principal.roles
        )

    async def fenced_operation(actor: WorkbenchActor, operation_id: str) -> OperationRecord | None:
        record = await runtime.read_operation(actor, operation_id)
        if record is None:
            return None
        if record.offer_id not in profile.offer_ids or not profile.owns_session_key(
            record.session_key
        ):
            return None
        return record

    @router.get("/operations/{operation_id}")
    async def read_operation(
        operation_id: str,
        request: Request,
        header_token: str | None = Header(default=None, alias=TOKEN_NAME),
        cookie_token: str | None = Cookie(default=None, alias=TOKEN_NAME),
    ) -> JSONResponse:
        if OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            return hidden(request)
        try:
            actor = await resolve_actor(header_token, cookie_token)
        except SessionUnavailable:
            return _unavailable(request, "SESSION")
        if actor is None:
            return hidden(request)
        try:
            record = await fenced_operation(actor, operation_id)
            if record is None:
                return hidden(request)
            detail = await build_detail(runtime, actor, record, now=now())
        except RuntimeUnavailable:
            return _unavailable(request, "RUNTIME")
        return JSONResponse(detail.model_dump(mode="json"))

    @router.post("/operations/{operation_id}/approval/decision")
    async def decide_operation(
        operation_id: str,
        body: ApprovalDecisionRequest,
        request: Request,
        header_token: str | None = Header(default=None, alias=TOKEN_NAME),
        cookie_token: str | None = Cookie(default=None, alias=TOKEN_NAME),
    ) -> JSONResponse:
        if OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            return hidden(request)
        try:
            actor = await resolve_actor(header_token, cookie_token)
        except SessionUnavailable:
            return _unavailable(request, "SESSION")
        if actor is None:
            return hidden(request)
        try:
            record = await fenced_operation(actor, operation_id)
            if record is None:
                return hidden(request)
            execution_id = record.status.execution_id
            execution = (
                await runtime.read_execution(actor, execution_id)
                if execution_id is not None
                else None
            )
            if (
                record.status.state != "waiting_approval"
                or execution is None
                or execution.waiting_approval_id != body.approval_id
            ):
                # The approval named by the page is not the operation's current
                # waiting item: stale page or a wrong id; never forwarded.
                return _error(
                    request,
                    409,
                    "CRM_APPROVAL_STALE",
                    "The approval is no longer the operation's pending item.",
                    category="conflict",
                )
            try:
                await runtime.decide(
                    actor,
                    approval_id=body.approval_id,
                    row_version=body.row_version,
                    decision=body.decision,
                    reason=body.reason.strip() or None,
                )
            except RuntimeDecisionRefused as refusal:
                return _refusal(request, refusal)
            after = await fenced_operation(actor, operation_id)
        except RuntimeUnavailable:
            return _unavailable(request, "RUNTIME")
        if after is None:
            # The decision was recorded; the fresh projection is what could not be read.
            return _unavailable(request, "RUNTIME")
        return JSONResponse({"status": after.document})

    return router


def _refusal(request: Request, refusal: RuntimeDecisionRefused) -> JSONResponse:
    if refusal.kind == "hidden":
        return hidden(request)
    if refusal.kind == "expired":
        return _error(
            request,
            410,
            "CRM_APPROVAL_EXPIRED",
            "The approval deadline has passed; the write was not performed.",
            category="permanent",
        )
    if refusal.kind == "forbidden":
        return _error(
            request,
            403,
            "CRM_APPROVAL_FORBIDDEN",
            "You are not an authorized approver for this item.",
            category="authorization",
        )
    return _error(
        request,
        409,
        "CRM_APPROVAL_CONFLICT",
        "The approval changed since it was read or is already decided.",
        category="conflict",
    )


__all__ = [
    "AUDIT_PATH",
    "approval_deadline",
    "build_detail",
    "hidden",
    "is_decidable",
    "operation_router",
    "project_approval",
    "project_evidence",
]
