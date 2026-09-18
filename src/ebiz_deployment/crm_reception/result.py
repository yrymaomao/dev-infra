"""Validation and safe display projection of the CRM ``case-advice-result`` envelope.

The schema copies under ``crm_reception/contracts`` are byte-for-byte the
``crm_agent`` wheel resources ``schemas/case-advice-result.schema.yaml`` and
``schemas/crm-advice-output.schema.yaml`` (``tests/test_crm_reception.py``
proves it whenever the wheel is importable). They are bundled rather than read
from the wheel so the BFF process never has to import the agent package.

The projection is a whitelist. Beside the envelope scope it keeps the advice
verdicts, codes, summaries the agent wrote and the reply draft's ``body``
verbatim (decision D-4: the draft is agent text). It drops every internal
reference (``content_ref``, ``target_thread_ref``, ``body_hash``,
``external_object_id``, ``capability_ref`` ...) and carries no buyer contact
detail or buyer message text because the result schema has no such field.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

PROJECTION_SCHEMA_VERSION = "crm.case-advice-projection.v1"
RESULT_SCHEMA = "case-advice-result.schema.yaml"
OUTPUT_SCHEMA = "crm-advice-output.schema.yaml"
_ASSESSMENT = ("status", "blocking_claims", "exceptions", "risk_flags")
_EXCEPTION = ("code", "importance", "claim", "detail")
_DECISION = (
    "next_action",
    "action_type",
    "priority",
    "reasons",
    "risk_flags",
    "requires_human",
    "summary",
)
_PROPOSAL = ("action_type", "impact_summary", "risk_summary", "channel_send_supported")
_DRAFT = ("subject", "locale", "body", "citations", "contains_commitment", "requested_information")
_HANDOFF = ("summary", "open_questions", "risk_flags")
_EVIDENCE = (
    "evidence_id",
    "source_type",
    "source_system",
    "captured_at",
    "freshness_at",
    "classification",
    "schema_version",
)
_SCOPE = (
    "account_id",
    "platform",
    "case_id",
    "case_version",
    "snapshot_hash",
    "policy_version",
    "produced_at",
)


def schema_bytes(name: str) -> bytes:
    if name not in (RESULT_SCHEMA, OUTPUT_SCHEMA):
        raise ValueError("unknown CRM reception schema")
    return (files("ebiz_deployment.crm_reception") / "contracts" / name).read_bytes()


@lru_cache(maxsize=1)
def result_validator() -> Draft202012Validator:
    documents = {
        name: yaml.safe_load(schema_bytes(name)) for name in (RESULT_SCHEMA, OUTPUT_SCHEMA)
    }
    registry: Registry[Any] = Registry().with_resources(
        (name, Resource.from_contents(document, default_specification=DRAFT202012))
        for name, document in documents.items()
    )
    return Draft202012Validator(
        documents[RESULT_SCHEMA],
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}


async def project_case_advice(repository: Any, tenant: str, value: object) -> dict[str, Any]:
    """Validate one advise result for ``tenant`` and project its safe display document."""

    del repository  # the envelope is inline; nothing is loaded from payload storage
    if not isinstance(value, dict) or value.get("tenant_id") != tenant:
        raise ValueError("Conversation result tenant mismatch")
    errors = sorted(result_validator().iter_errors(value), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "<root>"
        raise ValueError(f"Conversation result violates case-advice-result at {location}")
    scope = value["scope"]
    advice = value["payload"]
    if scope["tenant_id"] != tenant or any(
        ref["tenant_id"] != tenant for ref in advice["evidence_refs"]
    ):
        raise ValueError("Conversation result tenant mismatch")
    if scope["case_id"] != advice["case_id"] or scope["case_version"] != advice["case_version"]:
        raise ValueError("Conversation result scope does not match its advice")
    projected: dict[str, Any] = {
        "schema_version": advice["schema_version"],
        "case_id": advice["case_id"],
        "case_version": advice["case_version"],
        "outcome": advice["outcome"],
        "policy_version": advice["policy_version"],
        "snapshot_hash": advice["snapshot_hash"],
        "observed_at": advice["observed_at"],
        "assessment": {
            **_pick(advice["assessment"], _ASSESSMENT),
            "exceptions": [_pick(item, _EXCEPTION) for item in advice["assessment"]["exceptions"]],
        },
        "evidence_refs": [_pick(ref, _EVIDENCE) for ref in advice["evidence_refs"]],
    }
    if "decision" in advice:
        projected["decision"] = _pick(advice["decision"], _DECISION)
    if "proposal" in advice:
        projected["proposal"] = _pick(advice["proposal"], _PROPOSAL)
    if "draft" in advice:
        projected["draft"] = _pick(advice["draft"], _DRAFT)
    if "handoff" in advice:
        projected["handoff"] = _pick(advice["handoff"], _HANDOFF)
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "outcome": advice["outcome"],
        "scope": _pick(scope, _SCOPE),
        "advice": projected,
    }


__all__ = [
    "OUTPUT_SCHEMA",
    "PROJECTION_SCHEMA_VERSION",
    "RESULT_SCHEMA",
    "project_case_advice",
    "result_validator",
    "schema_bytes",
]
