"""Safe projection from canonical Runtime events to business-agent activity v1."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_TOOLS = frozenset(
    {
        "query_inventory_summary_v2",
        "query_sku_boston_cohort_v1",
        "query_sku_sales_profit_windows_v1",
        "query_sku_upc_mapping",
    }
)
_TOOLS_BY_NODE = {
    "resolve_identity": "query_sku_upc_mapping",
    "read_inventory": "query_inventory_summary_v2",
    "read_sales": "query_sku_sales_profit_windows_v1",
    "read_cohort": "query_sku_boston_cohort_v1",
}


@dataclass(frozen=True, slots=True)
class ActivityProjection:
    event_key: str
    execution_id: str
    execution_sequence: int
    event_type: str
    phase: str
    state: str
    safe_message: str
    tool_name: str | None
    duration_ms: int | None
    payload: dict[str, object]
    critical: bool


def project_runtime_event(event: Mapping[str, Any]) -> ActivityProjection:
    execution_id = str(event.get("execution_id", ""))
    sequence = int(event.get("sequence", 0))
    event_type = str(event.get("event_type", "unknown"))[:128]
    raw_payload = event.get("payload")
    source = raw_payload if isinstance(raw_payload, Mapping) else {}
    phase = _phase(event_type, source)
    state = _state(event_type)
    raw_tool = (
        source.get("tool_name")
        or source.get("capability")
        or _TOOLS_BY_NODE.get(str(source.get("node_id", "")))
    )
    tool_name = str(raw_tool) if isinstance(raw_tool, str) and raw_tool in _TOOLS else None
    raw_duration = source.get("duration_ms")
    duration_ms = (
        int(raw_duration)
        if isinstance(raw_duration, int)
        and not isinstance(raw_duration, bool)
        and raw_duration >= 0
        else None
    )
    safe_error = source.get("safe_message")
    message = (
        str(safe_error)[:1024]
        if state == "failed" and isinstance(safe_error, str) and safe_error
        else _message(phase, state, tool_name)
    )
    critical = state in {"failed", "completed", "waiting"} or event_type in {
        "session.started",
        "workflow.started",
    }
    safe_payload: dict[str, object] = {"schema_version": "business-agent.activity-event.v1"}
    error_code = source.get("error_code")
    if state == "failed" and isinstance(error_code, str):
        safe_payload["error_code"] = error_code[:128]
    retryable = source.get("retryable")
    if state == "failed" and isinstance(retryable, bool):
        safe_payload["retryable"] = retryable
    return ActivityProjection(
        event_key=f"runtime:{execution_id}:{sequence}",
        execution_id=execution_id,
        execution_sequence=sequence,
        event_type=event_type,
        phase=phase,
        state=state,
        safe_message=message,
        tool_name=tool_name,
        duration_ms=duration_ms,
        payload=safe_payload,
        critical=critical,
    )


def _phase(event_type: str, payload: Mapping[str, Any]) -> str:
    lowered = event_type.casefold()
    node = str(payload.get("node_id", "")).casefold()
    if "capability" in lowered or "tool" in lowered:
        return "mcp"
    if "model" in lowered:
        return "model"
    if "forecast" in lowered or "forecast" in node:
        return "forecast"
    if "decision" in lowered or "action" in node:
        return "decision"
    if "fail" in lowered or "cancel" in lowered:
        return "terminal"
    if "complete" in lowered:
        return "terminal"
    if "session" in lowered:
        return "validation"
    if "workflow" in lowered:
        return "dispatch"
    return "execution"


def _state(event_type: str) -> str:
    lowered = event_type.casefold()
    if "fail" in lowered:
        return "failed"
    if "cancel" in lowered:
        return "cancelled"
    if "wait" in lowered or "approval" in lowered:
        return "waiting"
    if "complete" in lowered or "succeed" in lowered:
        return "completed"
    if "start" in lowered or "request" in lowered:
        return "running"
    return "progress"


def _message(phase: str, state: str, tool_name: str | None) -> str:
    if tool_name is not None:
        return f"{tool_name} {state}."
    labels = {
        "validation": "Validating the request",
        "dispatch": "Dispatching the workflow",
        "mcp": "Reading supply chain data",
        "forecast": "Calculating the forecast",
        "model": "Analyzing the evidence",
        "decision": "Preparing the decision",
        "terminal": "Execution finished",
        "execution": "Execution is progressing",
    }
    return f"{labels[phase]} ({state})."
