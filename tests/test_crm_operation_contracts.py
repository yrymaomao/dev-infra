"""The BFF mirror of wire v2 ``OperationStatusV2`` against the P2-A shared fixtures.

``tests/fixtures/tool-gateway-v2/operation_status`` is a byte copy of the Runtime
repository's ``packages/shared-schemas/runtime/fixtures/tool-gateway-v2/operation_status``
(P2-A). Every accept case must validate and be forwarded unchanged; every reject
case must fail. Refresh the copy whenever P2-A changes the fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebiz_deployment.crm_reception.operation_contracts import (
    AUDIT_HREF_PATTERN,
    OPERATION_ID_PATTERN,
    ApprovalDecisionRequest,
    OperationDetail,
    parse_operation_status,
    safe_audit_href,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tool-gateway-v2" / "operation_status"
ACCEPT = sorted(FIXTURES.glob("accept/*.json"))
REJECT = sorted(FIXTURES.glob("reject/*.json"))


def test_fixture_copy_is_present() -> None:
    assert len(ACCEPT) == 10 and len(REJECT) == 19


@pytest.mark.parametrize("path", ACCEPT, ids=lambda p: p.stem)
def test_accept_fixtures_validate_and_are_forwarded_unchanged(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    status, forwarded = parse_operation_status(document)
    assert forwarded == document
    assert status.state == document["state"]
    assert status.revision == document["revision"]
    if document["state"] == "waiting_approval":
        assert status.interaction is not None
        assert status.interaction.kind == "approval"
        assert status.interaction.revision == document["interaction"]["revision"]
        assert status.interaction.path == document["interaction"]["path"]


@pytest.mark.parametrize("path", REJECT, ids=lambda p: p.stem)
def test_reject_fixtures_are_refused(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        parse_operation_status(document)


@pytest.mark.parametrize(
    "document",
    [
        "not an object",
        {"schema_version": "2"},
        {  # lax coercions are refused: strings never become numbers or booleans
            "schema_version": "2",
            "operation_id": "op-1",
            "state": "running",
            "revision": "3",
            "observed_at": "2026-09-16T02:00:00Z",
            "cancel_requested": False,
            "settlement": {"state": "not_started", "effect": "none"},
            "next_action": "poll_status",
            "retry_after_seconds": 5,
        },
        {  # naive timestamps are refused
            "schema_version": "2",
            "operation_id": "op-1",
            "state": "running",
            "revision": 3,
            "observed_at": "2026-09-16T02:00:00",
            "cancel_requested": False,
            "settlement": {"state": "not_started", "effect": "none"},
            "next_action": "poll_status",
            "retry_after_seconds": 5,
        },
    ],
)
def test_strictness_beyond_the_fixtures(document: object) -> None:
    with pytest.raises(ValueError):
        parse_operation_status(document)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("op-1", True),
        ("10000000-0000-4000-8000-000000000001", True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("../other", False),
        ("op 1?case_id=x", False),
        ("-leading", False),
        ("", False),
        ("op/1", False),
    ],
)
def test_operation_id_pattern(value: str, ok: bool) -> None:
    assert (OPERATION_ID_PATTERN.fullmatch(value) is not None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("/crm/audit?execution=exec-1", True),
        ("/crm/audit", True),
        ("https://evil.example/audit", False),
        ("//evil.example/audit", False),
        ("/crm/audit#token=abc", False),
        ("/../etc", False),
        ("/crm/../audit", False),
        ("crm/audit", False),
    ],
)
def test_audit_href_is_server_fixed_relative(value: str, ok: bool) -> None:
    assert (AUDIT_HREF_PATTERN.fullmatch(value) is not None and "/.." not in value) is ok
    if ok:
        assert safe_audit_href(value) == value
    else:
        with pytest.raises(ValueError):
            safe_audit_href(value)
        with pytest.raises(ValueError):
            OperationDetail(
                status={}, case=None, evidence=(), approval=None, effects=(), audit_href=value
            )


def test_decision_request_is_strict() -> None:
    body = ApprovalDecisionRequest(
        approval_id="approval-1", row_version=3, decision="APPROVED", reason="ok"
    )
    assert body.decision == "APPROVED"
    for broken in (
        {"approval_id": "approval-1", "row_version": 3, "decision": "MAYBE", "reason": ""},
        {"approval_id": "approval-1", "row_version": -1, "decision": "APPROVED", "reason": ""},
        {"approval_id": "approval-1", "row_version": "3", "decision": "APPROVED", "reason": ""},
        {"approval_id": "approval-1", "row_version": 3, "decision": "APPROVED"},
        {
            "approval_id": "approval-1",
            "row_version": 3,
            "decision": "APPROVED",
            "reason": "",
            "tenant_id": "other",
        },
    ):
        with pytest.raises(ValueError):
            ApprovalDecisionRequest.model_validate(broken)
