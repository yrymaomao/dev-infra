"""A valid ``case-advice-result`` envelope, as the advise workflow emits it."""

from __future__ import annotations

import copy
from typing import Any

TENANT = "tenant-a"
CASE_ID = "CASE-0042"
SHA = "a" * 64


def evidence_ref(*, tenant: str = TENANT, content_ref: str = "payload://evidence/1") -> dict:
    return {
        "evidence_id": "3f6c1a2e-9d2b-4c7e-8a1f-2b3c4d5e6f70",
        "tenant_id": tenant,
        "source_type": "case_record",
        "source_system": "crm-service",
        "external_object_id": "internal-object-9",
        "captured_at": "2026-09-16T00:00:00Z",
        "content_ref": content_ref,
        "content_hash": SHA,
        "schema_version": 1,
        "classification": "confidential",
    }


def advised_result(*, tenant: str = TENANT, outcome: str = "ADVISED") -> dict[str, Any]:
    advice: dict[str, Any] = {
        "schema_version": "crm.case-advice.v1",
        "case_id": CASE_ID,
        "case_version": 3,
        "outcome": outcome,
        "policy_version": "crm-policy-v1",
        "snapshot_hash": SHA,
        "observed_at": "2026-09-16T00:00:01Z",
        "assessment": {
            "status": "ACTIONABLE",
            "blocking_claims": [],
            "exceptions": [
                {
                    "code": "SHIPMENT_SNAPSHOT_STALE",
                    "importance": "degrading",
                    "claim": "shipment_status",
                    "detail": "shipment snapshot older than the policy freshness window",
                }
            ],
            "risk_flags": ["LOW_MODEL_CONFIDENCE"],
        },
        "evidence_refs": [evidence_ref(tenant=tenant)],
    }
    if outcome == "ADVISED":
        advice["decision"] = {
            "next_action": "SEND_MESSAGE",
            "action_type": "SEND_MESSAGE",
            "priority": "P2",
            "reasons": ["RETURN_WINDOW_OPEN"],
            "risk_flags": [],
            "requires_human": False,
            "summary": "Reply with the return instructions.",
        }
        advice["proposal"] = {
            "action_type": "SEND_MESSAGE",
            "impact_summary": "One buyer-visible reply.",
            "risk_summary": "None beyond the commitment in the draft.",
            "target_thread_ref": "thread://internal/77",
            "body_hash": SHA,
            "channel_send_supported": True,
        }
        advice["draft"] = {
            "subject": "Your return",
            "locale": "en-US",
            "body": "Hello, your return label is on its way.",
            "citations": ["payload://evidence/1"],
            "contains_commitment": True,
            "requested_information": [],
        }
    elif outcome == "BLOCKED":
        advice["assessment"] = {
            "status": "BLOCKED",
            "blocking_claims": ["order_identity", "shipment_status"],
            "exceptions": [
                {
                    "code": "ORDER_NOT_FOUND",
                    "importance": "blocking",
                    "claim": "order_identity",
                    "detail": "no order matched the Case reference",
                }
            ],
            "risk_flags": ["EVIDENCE_CONFLICT"],
        }
    else:
        advice["assessment"]["status"] = "HUMAN_TAKEOVER"
        advice["handoff"] = {
            "summary": "Buyer threatens a dispute; a person must reply.",
            "open_questions": ["Was the replacement shipped?"],
            "risk_flags": ["PLATFORM_DISPUTE"],
        }
    return {
        "tenant_id": tenant,
        "status": "COMPLETE",
        "scope": {
            "tenant_id": tenant,
            "account_id": "acct-1",
            "platform": "amazon",
            "case_id": CASE_ID,
            "case_version": 3,
            "snapshot_hash": SHA,
            "policy_version": "crm-policy-v1",
            "produced_at": "2026-09-16T00:00:02Z",
        },
        "payload": advice,
        "evidence": [],
        "issues": [],
    }


def mutated(result: dict[str, Any], **changes: Any) -> dict[str, Any]:
    value = copy.deepcopy(result)
    for dotted, replacement in changes.items():
        target = value
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = replacement
    return value
