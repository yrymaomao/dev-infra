"""Safe CSV projection for complete Supply Chain Level 2 reports."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from typing import Any

REPORT_EXPORT_COLUMNS = (
    "ordinal",
    "sku",
    "result_status",
    "fulfillment_mode",
    "fulfillment_source",
    "selected_action",
    "selected_discount_rate",
    "business_issue_codes",
    "runtime_error_code",
    "risk_flags",
)


def report_export_header() -> str:
    """Return the stable UTF-8 CSV header."""

    return _csv_line(REPORT_EXPORT_COLUMNS)


def report_export_row(item: Mapping[str, Any]) -> str:
    """Project one validated result item without exposing raw evidence or payloads."""

    decision = item.get("decision")
    decision_map = decision if isinstance(decision, Mapping) else {}
    issues = item.get("business_issues")
    issue_codes = (
        "|".join(
            str(issue.get("code", ""))
            for issue in issues
            if isinstance(issue, Mapping) and issue.get("code")
        )
        if isinstance(issues, list)
        else ""
    )
    runtime_error = item.get("runtime_error")
    runtime_error_map = runtime_error if isinstance(runtime_error, Mapping) else {}
    risk_flags = item.get("risk_flags")
    values = (
        item.get("ordinal", ""),
        item.get("sku", ""),
        item.get("result_status", ""),
        item.get("fulfillment_mode", ""),
        item.get("fulfillment_source", ""),
        decision_map.get("selected_action", ""),
        decision_map.get("selected_discount_rate", ""),
        issue_codes,
        runtime_error_map.get("error_code", ""),
        "|".join(str(value) for value in risk_flags) if isinstance(risk_flags, list) else "",
    )
    return _csv_line(values)


def _csv_line(values: tuple[object, ...]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow([_safe_cell(value) for value in values])
    return output.getvalue()


def _safe_cell(value: object) -> str:
    rendered = "" if value is None else str(value)
    if rendered.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{rendered}"
    return rendered
