"""P2-G workbench operation routes: pass-through, whitelist, decidable, decision mapping, A02."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import jwt
import pytest
from crm_fixtures import TENANT
from reception_fixtures import bff_settings

from ebiz_deployment.crm_reception.operation_contracts import parse_operation_status
from ebiz_deployment.crm_reception.operation_runtime import (
    ACTOR_TYPE,
    APPROVAL_DECISION_PATH,
    APPROVAL_PATH,
    EXECUTION_PATH,
    WORKBENCH_OPERATION_PATH,
    WRITE_SETTLEMENT_PATH,
    ApprovalPreview,
    ApprovalRecordView,
    DecisionRefusal,
    EffectRecord,
    ExecutionRecord,
    HttpOperationRuntime,
    OperationRecord,
    RuntimeDecisionRefused,
    RuntimeUnavailable,
    SettlementRecord,
    WorkbenchActor,
)
from ebiz_deployment.crm_reception.session_api import SessionInvalid, SessionPrincipal
from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner

OP = "op-1"
EXEC = "exec-1"
APPROVAL = "approval-1"
BASE = "/api/crm/v2/openclaw/operations"
NOW = datetime(2026, 9, 16, 2, 10, tzinfo=UTC)
SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
FORBIDDEN_STRINGS = (
    "buyer@example.com",
    "+1-555-0100",
    "Where is my parcel, I am furious",
    "payload://evidence/1",
    "Buyer Person",
)
_ENVELOPE_KEYS = {"error_code", "phase", "category", "retryable", "safe_message", "request_id"}


# --- fixtures ---------------------------------------------------------------------


def status_doc(state: str = "waiting_approval", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "2",
        "operation_id": OP,
        "state": state,
        "revision": 7,
        "observed_at": "2026-09-16T02:00:00Z",
        "execution_id": EXEC,
        "execution_deadline_at": "2026-09-16T02:30:00Z",
        "cancel_requested": False,
        "settlement": {"state": "not_started", "effect": "none"},
    }
    guidance: dict[str, dict[str, Any]] = {
        "pending": {"next_action": "poll_status", "retry_after_seconds": 5},
        "running": {"next_action": "poll_status", "retry_after_seconds": 5},
        "waiting_approval": {
            "next_action": "open_ebizhub",
            "retry_after_seconds": 30,
            "interaction": {
                "kind": "approval",
                "revision": 7,
                "path": f"/crm/operations/{OP}",
                "label": "前往 eBizHub 审批",
            },
        },
        "waiting_input": {
            "next_action": "open_ebizhub",
            "retry_after_seconds": 30,
            "interaction": {
                "kind": "input",
                "revision": 7,
                "path": f"/crm/operations/{OP}",
                "label": "补充信息",
            },
        },
        "waiting_external": {"next_action": "poll_status", "retry_after_seconds": 5},
        "reconciling": {
            "next_action": "open_ebizhub",
            "retry_after_seconds": 30,
            "interaction": {
                "kind": "review",
                "revision": 7,
                "path": f"/crm/operations/{OP}",
                "label": "人工核查",
            },
            "settlement": {"state": "pending", "effect": "unknown"},
        },
        "completed": {
            "next_action": "read_result",
            "settlement": {"state": "resolved", "effect": "confirmed"},
        },
        "error": {
            "next_action": "none",
            "settlement": {"state": "resolved", "effect": "partial"},
            "error": {
                "error_code": "OPERATION_FAILED",
                "category": "permanent",
                "phase": "invocation",
                "retryable": False,
                "safe_message": "the operation failed",
                "trace_id": "10000000-0000-4000-8000-000000000001",
            },
        },
        "cancelled": {
            "next_action": "none",
            "settlement": {"state": "resolved", "effect": "none"},
            "cancel_requested": True,
        },
    }
    base.update(guidance[state])
    base.update(overrides)
    return base


def record(
    doc: dict[str, Any],
    *,
    offer_id: str = "crm-case-advice",
    session_key: str = "agent:crm:openclaw:s1",
) -> OperationRecord:
    status, document = parse_operation_status(doc)
    return OperationRecord(
        status=status, document=document, offer_id=offer_id, session_key=session_key
    )


def raw_preview() -> ApprovalPreview:
    """What the owner's preview carries - deliberately including things the page must not see."""

    return ApprovalPreview(
        case_id="CASE-0042",
        case_version=12,
        proposal={
            "action_type": "SEND_MESSAGE",
            "impact_summary": "One buyer-visible reply.",
            "risk_summary": "Low",
            "target_thread_ref": "thread://internal/77",
            "body_hash": SHA_C,
            "channel_send_supported": True,
            "buyer_name": "Buyer Person",
        },
        draft={
            "subject": "Your return",
            "locale": "en-US",
            "body": "Hello, your return label is on its way.",
            "citations": ["payload://evidence/1"],
            "contains_commitment": True,
            "buyer_email": "buyer@example.com",
        },
        evidence=(
            {
                "evidence_id": "3f6c1a2e-9d2b-4c7e-8a1f-2b3c4d5e6f70",
                "source_type": "case_record",
                "source_system": "crm-service",
                "summary": "Order shipped 2026-09-10",
                "content_ref": "payload://evidence/1",
                "buyer_email": "buyer@example.com",
                "buyer_phone": "+1-555-0100",
            },
            {
                "evidence_id": "4f6c1a2e-9d2b-4c7e-8a1f-2b3c4d5e6f71",
                "source_type": "conversation",
                "summary": "Buyer asked for a return",
                "conversation_text": "Where is my parcel, I am furious",
            },
            {"source_type": "orphan-without-ref", "summary": "dropped"},
        ),
    )


def approval_view(
    *,
    status: str = "PENDING",
    approver_roles: tuple[str, ...] = ("CRM_SUPERVISOR",),
    expires_at: datetime = NOW + timedelta(minutes=20),
    usable_until: datetime | None = NOW + timedelta(minutes=10),
    preview: ApprovalPreview | None = None,
    execution_id: str = EXEC,
) -> ApprovalRecordView:
    return ApprovalRecordView(
        approval_id=APPROVAL,
        execution_id=execution_id,
        status=status,
        row_version=3,
        node_id="send_message",
        capability_ref="crm.send_case_message@1",
        input_hash=SHA_A,
        preview_hash=SHA_B,
        expires_at=expires_at,
        approval_usable_until=usable_until,
        approver_roles=approver_roles,
        preview=preview if preview is not None else raw_preview(),
    )


def execution_view(waiting: bool = True) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=EXEC,
        status="WAITING_APPROVAL" if waiting else "RUNNING",
        waiting_approval_id=APPROVAL if waiting else None,
        waiting_row_version=3 if waiting else None,
        outputs=None,
    )


@dataclass
class FakeRuntime:
    operations: dict[str, OperationRecord] = field(default_factory=dict)
    executions: dict[str, ExecutionRecord] = field(default_factory=dict)
    approvals: dict[str, ApprovalRecordView] = field(default_factory=dict)
    settlements: dict[str, SettlementRecord] = field(default_factory=dict)
    refusal: DecisionRefusal | None = None
    after_decision: OperationRecord | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    actors: list[WorkbenchActor] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    async def read_operation(
        self, actor: WorkbenchActor, operation_id: str
    ) -> OperationRecord | None:
        self.calls.append(("operation", operation_id))
        self.actors.append(actor)
        return self.operations.get(operation_id)

    async def read_execution(
        self, actor: WorkbenchActor, execution_id: str
    ) -> ExecutionRecord | None:
        self.calls.append(("execution", execution_id))
        return self.executions.get(execution_id)

    async def read_approval(
        self, actor: WorkbenchActor, approval_id: str
    ) -> ApprovalRecordView | None:
        self.calls.append(("approval", approval_id))
        return self.approvals.get(approval_id)

    async def read_settlement(
        self, actor: WorkbenchActor, execution_id: str
    ) -> SettlementRecord | None:
        self.calls.append(("settlement", execution_id))
        return self.settlements.get(execution_id)

    async def decide(
        self,
        actor: WorkbenchActor,
        *,
        approval_id: str,
        row_version: int,
        decision: Literal["APPROVED", "REJECTED"],
        reason: str | None,
    ) -> None:
        self.calls.append(("decide", approval_id))
        self.decisions.append(
            {
                "actor": actor,
                "approval_id": approval_id,
                "row_version": row_version,
                "decision": decision,
                "reason": reason,
            }
        )
        if self.refusal is not None:
            raise RuntimeDecisionRefused(self.refusal)
        if self.after_decision is not None:
            self.operations[OP] = self.after_decision
            self.executions[EXEC] = execution_view(waiting=False)


def waiting_runtime(**kwargs: Any) -> FakeRuntime:
    return FakeRuntime(
        operations={OP: record(status_doc("waiting_approval"))},
        executions={EXEC: execution_view()},
        approvals={APPROVAL: approval_view()},
        settlements={
            EXEC: SettlementRecord(
                effects=(
                    EffectRecord(
                        node_key="record_decision",
                        status="CONFIRMED",
                        result_ref="receipt-1",
                        observed_at=datetime(2026, 9, 16, 2, 1, tzinfo=UTC),
                    ),
                    EffectRecord(
                        node_key="odd", status="NOT_A_STATE", result_ref=None, observed_at=NOW
                    ),
                )
            )
        },
        after_decision=record(status_doc("running", revision=8)),
        **kwargs,
    )


class FakeSessionClient:
    def __init__(self, outcome: SessionPrincipal | Exception) -> None:
        self.outcome = outcome
        self.tokens: list[str] = []

    async def resolve(self, token: str) -> SessionPrincipal:
        self.tokens.append(token)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def supervisor(
    tenant: str = TENANT, roles: tuple[str, ...] = ("CRM_SUPERVISOR",)
) -> SessionPrincipal:
    return SessionPrincipal(tenant, "agent-7", roles)


def settings(**overrides: object) -> BffSettings:
    values: dict[str, object] = dict(
        openclaw_enabled=True,
        openclaw_connector_credential="c" * 32,
        tool_gateway_jwt_key="g" * 32,
        crm_openclaw_enabled=True,
        crm_openclaw_tenant_id=TENANT,
        crm_openclaw_ingress_credential="s" * 32,
        crm_workbench_operations_enabled=True,
    )
    values.update(overrides)
    return bff_settings(**values)


def make_app(
    runtime: FakeRuntime, session: SessionPrincipal | Exception, **overrides: object
) -> tuple[Any, FakeSessionClient]:
    client = FakeSessionClient(session)
    app = create_app(
        BffContainer(
            settings=settings(**overrides),
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            crm_session_client=client,
            crm_operation_runtime=runtime,
        )
    )
    return app, client


def http(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={"_token_": "erp"},
    )


def decision_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "approval_id": APPROVAL,
        "row_version": 3,
        "decision": "APPROVED",
        "reason": "Looks right",
    }
    body.update(overrides)
    return body


# --- clock injection: the router reads ``now`` through operation_api ------------


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    import ebiz_deployment.crm_reception.operation_api as module

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return NOW

    monkeypatch.setattr(module, "datetime", _Clock)


# --- GET ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        "pending",
        "running",
        "waiting_approval",
        "waiting_input",
        "waiting_external",
        "reconciling",
        "completed",
        "error",
        "cancelled",
    ],
)
async def test_nine_states_pass_through_unchanged(state: str) -> None:
    doc = status_doc(state)
    runtime = waiting_runtime()
    runtime.operations[OP] = record(doc)
    if state != "waiting_approval":
        runtime.executions[EXEC] = execution_view(waiting=False)
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == doc
    if state == "waiting_approval":
        assert body["status"]["interaction"] == {
            "kind": "approval",
            "revision": 7,
            "path": f"/crm/operations/{OP}",
            "label": "前往 eBizHub 审批",
        }
        assert body["approval"]["decidable"] is True
    else:
        assert body["approval"] is None
    assert body["case"] == (
        {"case_id": "CASE-0042", "case_version": 12} if state == "waiting_approval" else None
    )
    assert body["audit_href"] == f"/crm/audit?execution={EXEC}"
    assert body["effects"] == [
        {
            "node_ref": "record_decision",
            "journal_state": "CONFIRMED",
            "receipt_ref": "receipt-1",
            "observed_at": "2026-09-16T02:01:00Z",
        }
    ]


async def test_unmapped_operation_has_no_execution_facts() -> None:
    runtime = FakeRuntime(
        operations={
            OP: record(status_doc("pending", execution_id=None, execution_deadline_at=None))
        }
    )
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"]["execution_id"] is None
    assert body["case"] is None and body["approval"] is None
    assert body["evidence"] == [] and body["effects"] == [] and body["audit_href"] is None
    assert [kind for kind, _ in runtime.calls] == ["operation"]


async def test_evidence_and_draft_are_whitelisted() -> None:
    app, _ = make_app(waiting_runtime(), supervisor())
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.status_code == 200
    body = response.json()
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden not in response.text, forbidden
    assert body["evidence"] == [
        {
            "source_type": "case_record",
            "ref": "3f6c1a2e-9d2b-4c7e-8a1f-2b3c4d5e6f70",
            "summary": "Order shipped 2026-09-10",
        },
        {
            "source_type": "conversation",
            "ref": "4f6c1a2e-9d2b-4c7e-8a1f-2b3c4d5e6f71",
            "summary": "Buyer asked for a return",
        },
    ]
    approval = body["approval"]
    assert approval["draft"] == {
        "body": "Hello, your return label is on its way.",
        "language": "en-US",
    }
    assert approval["proposal"] == {
        "action_type": "SEND_MESSAGE",
        "impact_summary": "One buyer-visible reply.",
        "risk_summary": "Low",
        "target_thread_ref": "thread://internal/77",
        "body_hash": SHA_C,
    }
    assert set(approval) == {
        "approval_id",
        "row_version",
        "node_ref",
        "capability_ref",
        "input_hash",
        "preview_hash",
        "proposal",
        "draft",
        "deadline_at",
        "decidable",
    }
    assert approval["approval_id"] == APPROVAL and approval["row_version"] == 3
    assert approval["node_ref"] == "send_message"
    assert approval["capability_ref"] == "crm.send_case_message@1"
    assert approval["input_hash"] == SHA_A and approval["preview_hash"] == SHA_B
    # deadline = min(approval_usable_until, expires_at)
    assert approval["deadline_at"] == "2026-09-16T02:20:00Z"


@pytest.mark.parametrize(
    ("approval_status", "state", "roles", "usable_until", "expected"),
    [
        ("PENDING", "waiting_approval", ("CRM_SUPERVISOR",), NOW + timedelta(minutes=10), True),
        ("PENDING", "waiting_approval", ("CRM_AGENT",), NOW + timedelta(minutes=10), False),
        ("PENDING", "waiting_approval", (), NOW + timedelta(minutes=10), False),
        ("PENDING", "waiting_approval", ("CRM_SUPERVISOR",), NOW - timedelta(seconds=1), False),
        ("APPROVED", "waiting_approval", ("CRM_SUPERVISOR",), NOW + timedelta(minutes=10), False),
        ("EXPIRED", "waiting_approval", ("CRM_SUPERVISOR",), NOW + timedelta(minutes=10), False),
        ("PENDING", "reconciling", ("CRM_SUPERVISOR",), NOW + timedelta(minutes=10), False),
    ],
)
async def test_decidable_matrix(
    approval_status: str,
    state: str,
    roles: tuple[str, ...],
    usable_until: datetime,
    expected: bool,
) -> None:
    runtime = waiting_runtime()
    runtime.operations[OP] = record(status_doc(state))
    runtime.approvals[APPROVAL] = approval_view(status=approval_status, usable_until=usable_until)
    app, _ = make_app(runtime, supervisor(roles=roles))
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.status_code == 200, response.text
    assert response.json()["approval"]["decidable"] is expected


async def test_deadline_falls_back_to_the_approval_ttl() -> None:
    runtime = waiting_runtime()
    runtime.approvals[APPROVAL] = approval_view(usable_until=None)
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.json()["approval"]["deadline_at"] == "2026-09-16T02:30:00Z"


async def test_approval_bound_to_another_execution_is_not_shown() -> None:
    runtime = waiting_runtime()
    runtime.approvals[APPROVAL] = approval_view(execution_id="exec-other")
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.status_code == 200
    assert response.json()["approval"] is None


async def test_runtime_outage_is_503_not_404() -> None:
    class Broken(FakeRuntime):
        async def read_operation(
            self, actor: WorkbenchActor, operation_id: str
        ) -> OperationRecord | None:
            raise RuntimeUnavailable()

    app, _ = make_app(Broken(), supervisor())
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.status_code == 503
    assert response.json()["retryable"] is True


# --- POST -----------------------------------------------------------------------


async def test_decision_is_forwarded_with_the_session_actor_and_status_is_reread() -> None:
    runtime = waiting_runtime()
    app, client_stub = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.post(
            f"{BASE}/{OP}/approval/decision",
            json=decision_body(reason="  Looks right "),
            headers={"_token_": "header-token"},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": status_doc("running", revision=8)}
    assert client_stub.tokens == ["header-token"]  # the header wins over the cookie
    (decision,) = runtime.decisions
    assert decision["approval_id"] == APPROVAL
    assert decision["row_version"] == 3
    assert decision["decision"] == "APPROVED"
    assert decision["reason"] == "Looks right"
    actor = decision["actor"]
    assert actor == WorkbenchActor(tenant_id=TENANT, actor_id="agent-7", roles=("CRM_SUPERVISOR",))
    assert [kind for kind, _ in runtime.calls] == ["operation", "execution", "decide", "operation"]


async def test_reject_stays_reject_and_empty_reason_is_none() -> None:
    runtime = waiting_runtime()
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.post(
            f"{BASE}/{OP}/approval/decision", json=decision_body(decision="REJECTED", reason="")
        )
    assert response.status_code == 200
    assert runtime.decisions[0]["decision"] == "REJECTED"
    assert runtime.decisions[0]["reason"] is None


@pytest.mark.parametrize(
    ("refusal", "status"),
    [("conflict", 409), ("expired", 410), ("forbidden", 403), ("hidden", 404)],
)
async def test_runtime_refusals_map_to_workbench_statuses(
    refusal: DecisionRefusal, status: int
) -> None:
    runtime = waiting_runtime(refusal=refusal)
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.post(f"{BASE}/{OP}/approval/decision", json=decision_body())
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == _ENVELOPE_KEYS
    if status == 404:
        assert body["error_code"] == "CRM_OPERATION_NOT_FOUND"
    assert len(runtime.decisions) == 1


async def test_stale_approval_id_is_409_and_never_forwarded() -> None:
    runtime = waiting_runtime()
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.post(
            f"{BASE}/{OP}/approval/decision", json=decision_body(approval_id="approval-old")
        )
    assert response.status_code == 409
    assert response.json()["error_code"] == "CRM_APPROVAL_STALE"
    assert runtime.decisions == []


async def test_operation_not_waiting_is_409_and_never_forwarded() -> None:
    runtime = waiting_runtime()
    runtime.operations[OP] = record(status_doc("running"))
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.post(f"{BASE}/{OP}/approval/decision", json=decision_body())
    assert response.status_code == 409
    assert runtime.decisions == []


@pytest.mark.parametrize(
    "body",
    [
        decision_body(decision="MAYBE"),
        decision_body(row_version="3"),
        decision_body(tenant_id="other"),
        {k: v for k, v in decision_body().items() if k != "reason"},
    ],
)
async def test_malformed_decision_bodies_are_rejected_before_any_call(body: dict[str, Any]) -> None:
    runtime = waiting_runtime()
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.post(f"{BASE}/{OP}/approval/decision", json=body)
    assert response.status_code == 422
    assert runtime.calls == []


# --- A02: identical hiding ---------------------------------------------------------


def _hidden_cases() -> list[tuple[str, Any]]:
    other_offer = waiting_runtime()
    other_offer.operations[OP] = record(status_doc(), offer_id="supply-chain-on-demand")
    other_agent = waiting_runtime()
    other_agent.operations[OP] = record(status_doc(), session_key="agent:main:openclaw:s1")
    return [
        ("unknown", (FakeRuntime(), supervisor(), True)),
        ("foreign-tenant-session", (waiting_runtime(), supervisor(tenant="tenant-b"), True)),
        ("other-offer", (other_offer, supervisor(), True)),
        ("other-agent-session-key", (other_agent, supervisor(), True)),
        ("no-session-token", (waiting_runtime(), supervisor(), False)),
        ("session-401", (waiting_runtime(), SessionInvalid(401), True)),
        ("session-403-machine", (waiting_runtime(), SessionInvalid(403), True)),
    ]


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a02_unknown_foreign_other_profile_and_no_session_are_one_404(method: str) -> None:
    bodies: dict[str, Any] = {}
    for name, (runtime, session, with_token) in _hidden_cases():
        app, _ = make_app(runtime, session)
        headers = {"x-request-id": "req-1"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bff.test",
            cookies={"_token_": "erp"} if with_token else None,
        ) as client:
            if method == "GET":
                response = await client.get(f"{BASE}/{OP}", headers=headers)
            else:
                response = await client.post(
                    f"{BASE}/{OP}/approval/decision", json=decision_body(), headers=headers
                )
        assert response.status_code == 404, (name, response.text)
        bodies[name] = response.json()
        assert "tenant-b" not in response.text and "supply-chain" not in response.text
        assert runtime.decisions == [], name
    assert len({json.dumps(body, sort_keys=True) for body in bodies.values()}) == 1, bodies
    assert set(next(iter(bodies.values()))) == _ENVELOPE_KEYS


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_no_runtime_call_before_the_tenant_fence(method: str) -> None:
    for runtime, session, with_token in (
        (waiting_runtime(), supervisor(tenant="tenant-b"), True),
        (waiting_runtime(), supervisor(), False),
        (waiting_runtime(), SessionInvalid(401), True),
    ):
        app, _ = make_app(runtime, session)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bff.test",
            cookies={"_token_": "erp"} if with_token else None,
        ) as client:
            if method == "GET":
                await client.get(f"{BASE}/{OP}")
            else:
                await client.post(f"{BASE}/{OP}/approval/decision", json=decision_body())
        assert runtime.calls == []


async def test_other_profile_operation_stops_at_the_owner_read() -> None:
    runtime = waiting_runtime()
    runtime.operations[OP] = record(status_doc(), offer_id="supply-chain-on-demand")
    app, _ = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.status_code == 404
    assert runtime.calls == [("operation", OP)]


@pytest.mark.parametrize("bad", ["..%2Fother", "op%201", "-leading", "a" * 129, "op%2F1"])
async def test_operation_id_pattern_is_enforced_before_any_call(bad: str) -> None:
    runtime = waiting_runtime()
    app, session_client = make_app(runtime, supervisor())
    async with http(app) as client:
        response = await client.get(f"{BASE}/{bad}")
        post = await client.post(f"{BASE}/{bad}/approval/decision", json=decision_body())
    assert response.status_code == 404 and post.status_code == 404
    assert runtime.calls == [] and session_client.tokens == []


async def test_routes_are_absent_unless_enabled() -> None:
    runtime = waiting_runtime()
    app, _ = make_app(runtime, supervisor(), crm_workbench_operations_enabled=False)
    async with http(app) as client:
        response = await client.get(f"{BASE}/{OP}")
    assert response.status_code == 404
    assert runtime.calls == []


def test_settings_flag_requires_the_crm_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "BFF_POSTGRESQL_URL": "postgresql+asyncpg://u:p@127.0.0.1/db",
        "BFF_CURSOR_HMAC_SIGNING_KEY": "k" * 32,
        "APP_JWT_SECRET": "j" * 32,
        "BFF_RUNTIME_URL": "http://127.0.0.1:8000",
        "SUPPLY_CHAIN_SKILL_INPUT_REF": "payload://skill/current",
        "SUPPLY_CHAIN_CREDENTIAL_REF": "opaque:runtime-service",
        "BFF_CRM_WORKBENCH_OPERATIONS_ENABLED": "true",
        "BFF_CRM_OPENCLAW_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="BFF_CRM_WORKBENCH_OPERATIONS_ENABLED requires"):
        BffSettings.from_environment()


# --- HttpOperationRuntime: credential path and Runtime error mapping -------------------

SECRET = "j" * 32
ACTOR = WorkbenchActor(tenant_id=TENANT, actor_id="agent-7", roles=("CRM_SUPERVISOR",))


def runtime_over(handler: Any) -> HttpOperationRuntime:
    return HttpOperationRuntime(
        base_url="http://127.0.0.1:8000", secret=SECRET, transport=httpx.MockTransport(handler)
    )


def claims_of(request: httpx.Request) -> dict[str, Any]:
    token = request.headers["Authorization"].removeprefix("Bearer ")
    return jwt.decode(token, SECRET, algorithms=["HS256"], audience="agent-runtime")


async def test_http_runtime_reads_carry_a_human_actor_token_with_read_scope_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == WORKBENCH_OPERATION_PATH.format(operation_id=OP):
            return httpx.Response(
                200,
                json={
                    "status": status_doc(),
                    "offer_id": "crm-case-advice",
                    "session_key": "agent:crm:openclaw:s1",
                    "extra": "ignored",
                },
            )
        if request.url.path == EXECUTION_PATH.format(execution_id=EXEC):
            return httpx.Response(
                200,
                json={
                    "execution_id": EXEC,
                    "status": "WAITING_APPROVAL",
                    "outcome": None,
                    "outputs": None,
                    "waiting": {
                        "node_id": "send_message",
                        "reason": "WAITING_APPROVAL",
                        "approval_id": APPROVAL,
                        "approval_row_version": 3,
                    },
                    "error": None,
                    "event_sequence": 9,
                },
            )
        if request.url.path == APPROVAL_PATH.format(approval_id=APPROVAL):
            return httpx.Response(
                200,
                json={
                    "approval_id": APPROVAL,
                    "execution_id": EXEC,
                    "status": "PENDING",
                    "row_version": 3,
                    "node_id": "send_message",
                    "capability_ref": "crm.send_case_message@1",
                    "input_hash": SHA_A,
                    "preview_hash": SHA_B,
                    "expires_at": "2026-09-16T02:30:00Z",
                    "approval_usable_until": "2026-09-16T02:20:00Z",
                    "approver_roles": ["CRM_SUPERVISOR"],
                    "preview": {"case_id": "CASE-0042", "case_version": 12, "evidence_refs": []},
                },
            )
        if request.url.path == WRITE_SETTLEMENT_PATH.format(execution_id=EXEC):
            return httpx.Response(
                200,
                json={
                    "settlement": {"state": "not_started", "effect": "none"},
                    "effects": [
                        {
                            "node_key": "record_decision",
                            "status": "CONFIRMED",
                            "result_ref": "receipt-1",
                            "observed_at": "2026-09-16T02:01:00Z",
                        }
                    ],
                },
            )
        return httpx.Response(500)

    runtime = runtime_over(handler)
    operation = await runtime.read_operation(ACTOR, OP)
    execution = await runtime.read_execution(ACTOR, EXEC)
    approval = await runtime.read_approval(ACTOR, APPROVAL)
    settlement = await runtime.read_settlement(ACTOR, EXEC)
    assert operation is not None and operation.document == status_doc()
    assert operation.offer_id == "crm-case-advice"
    assert execution is not None and execution.waiting_approval_id == APPROVAL
    assert approval is not None and approval.approver_roles == ("CRM_SUPERVISOR",)
    assert approval.preview is not None and approval.preview.case_version == 12
    assert settlement is not None and settlement.effects[0].node_key == "record_decision"
    assert [r.method for r in seen] == ["GET"] * 4
    for request in seen:
        claims = claims_of(request)
        assert claims["sub"] == ACTOR.runtime_actor_id
        assert claims["actor_type"] == ACTOR_TYPE == "HUMAN"
        assert claims["tenant_id"] == TENANT
        assert claims["roles"] == ["CRM_SUPERVISOR"]
        assert claims["scopes"] == ["workflow:read"]
        assert claims["exp"] - claims["iat"] == 60
        assert "credential_ref" not in claims
        assert "Cookie" not in request.headers and "_token_" not in request.headers


async def test_http_runtime_decision_body_scope_and_error_mapping() -> None:
    outcomes: list[tuple[int, dict[str, Any]]] = [
        (
            200,
            {
                "approval_id": APPROVAL,
                "status": "APPROVED",
                "outcome": "APPROVED",
                "row_version": 4,
            },
        ),
        (409, {"error_code": "RUNTIME_STATE_CONFLICT", "category": "conflict"}),
        (409, {"error_code": "APPROVAL_EXPIRED", "category": "permanent"}),
        (403, {"error_code": "APPROVAL_ROLE_DENIED", "category": "authorization"}),
        (403, {"error_code": "PERMISSION_DENIED", "category": "authorization"}),
        (404, {"error_code": "RESOURCE_NOT_FOUND", "category": "permanent"}),
        (503, {"error_code": "RUNTIME_UNAVAILABLE", "category": "transient"}),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = outcomes[len(seen) - 1]
        return httpx.Response(status, json=body)

    runtime = runtime_over(handler)
    await runtime.decide(
        ACTOR, approval_id=APPROVAL, row_version=3, decision="APPROVED", reason="ok"
    )
    assert seen[0].url.path == APPROVAL_DECISION_PATH.format(approval_id=APPROVAL)
    assert json.loads(seen[0].content) == {
        "outcome": "APPROVED",
        "reason": "ok",
        "expected_row_version": 3,
    }
    assert claims_of(seen[0])["scopes"] == ["workflow:read", "approval:decide"]
    for expected in ("conflict", "expired", "forbidden", "forbidden", "hidden"):
        with pytest.raises(RuntimeDecisionRefused) as refused:
            await runtime.decide(
                ACTOR, approval_id=APPROVAL, row_version=3, decision="REJECTED", reason=None
            )
        assert refused.value.kind == expected
    with pytest.raises(RuntimeUnavailable):
        await runtime.decide(
            ACTOR, approval_id=APPROVAL, row_version=3, decision="REJECTED", reason=None
        )


async def test_http_runtime_hides_404_and_refuses_malformed_or_mismatched_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"error_code": "RESOURCE_NOT_FOUND"})
        if request.url.path.endswith("/mismatch"):
            return httpx.Response(
                200, json={"status": status_doc(), "offer_id": "x", "session_key": "y"}
            )
        return httpx.Response(
            200,
            json={
                "status": {**status_doc(), "state": "WAITING_APPROVAL"},
                "offer_id": "x",
                "session_key": "y",
            },
        )

    runtime = runtime_over(handler)
    assert await runtime.read_operation(ACTOR, "missing") is None
    with pytest.raises(RuntimeUnavailable):
        await runtime.read_operation(ACTOR, "mismatch")
    with pytest.raises(RuntimeUnavailable):
        await runtime.read_operation(ACTOR, OP)


def test_http_runtime_refuses_unsafe_origins_and_short_secrets() -> None:
    with pytest.raises(ValueError):
        HttpOperationRuntime(base_url="http://runtime.internal:8000", secret=SECRET)
    with pytest.raises(ValueError):
        HttpOperationRuntime(base_url="https://runtime.example.com", secret="short")
    HttpOperationRuntime(base_url="https://runtime.example.com", secret=SECRET)
