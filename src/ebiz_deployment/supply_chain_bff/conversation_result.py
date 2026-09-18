"""Report-v2 validation and safe display projection for conversation operations."""

import re
from typing import Any
from uuid import UUID

from .batch_result_contract import validated_batch_artifact, validated_batch_output

_UNQUALIFIED_CURRENCY_AMOUNT = re.compile(
    r"(?<![A-Za-z0-9])(?:[$€£¥￥]\s*)(-?\d+(?:,\d{3})*(?:\.\d+)?)"
)


def analysis_has_explicit_currency(value: object) -> bool:
    if isinstance(value, dict):
        for key, part in value.items():
            if key.lower() in {"currency", "currency_code"} and isinstance(part, str) and part:
                return True
            if analysis_has_explicit_currency(part):
                return True
    elif isinstance(value, list):
        return any(analysis_has_explicit_currency(part) for part in value)
    return False


def qualify_unowned_currency_symbols(text: str, analyses: object) -> str:
    """Remove model-inferred currency symbols when the verified result owns no currency fact."""
    if not analyses or analysis_has_explicit_currency(analyses):
        return text
    return _UNQUALIFIED_CURRENCY_AMOUNT.sub(r"\1 (currency unspecified)", text)


async def project_analysis(repository: Any, tenant: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("tenant_id") != tenant:
        raise ValueError("Conversation result tenant mismatch")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Conversation result unavailable")
    item_count = payload.get("item_count")
    if not isinstance(item_count, int) or isinstance(item_count, bool):
        raise ValueError("Conversation result item count invalid")
    output = validated_batch_output({"outputs": {"result": value}}, expected_item_count=item_count)
    artifact = output.get("result_artifact")
    if artifact is None:
        artifact = await repository._load(
            tenant, output["result_artifact_ref"], output["result_artifact_hash"]
        )
    validated = validated_batch_artifact(
        artifact,
        report_run_id=UUID(artifact["report_run_id"]),
        batch_id=UUID(artifact["batch_id"]),
        item_offset=artifact["item_offset"],
        expected_item_count=output["item_count"],
        expected_counts=(output["complete_count"], output["blocked_count"], output["failed_count"]),
    )
    items = []
    for row in validated["items"]:
        item = {
            key: row[key]
            for key in (
                "sku",
                "ordinal",
                "result_status",
                "fulfillment_mode",
                "fulfillment_source",
                "business_issues",
                "risk_flags",
                "evidence_refs",
                "runtime_error",
            )
            if key in row
        }
        forecast = row.get("forecast")
        if isinstance(forecast, dict):
            item["forecast"] = {
                key: forecast[key]
                for key in (
                    "schema_version",
                    "sku",
                    "model_version",
                    "data_cutoff",
                    "feature_snapshot_ref",
                    "risk_flags",
                    "weeks",
                )
                if key in forecast
            }
            feature_snapshot_ref = item["forecast"].get("feature_snapshot_ref")
            if isinstance(feature_snapshot_ref, str) and "://" in feature_snapshot_ref:
                item["forecast"].pop("feature_snapshot_ref")
        decision = row.get("decision")
        if isinstance(decision, dict):
            # The decision is already versioned/schema-validated; references are not UI fields.
            item["decision"] = {
                key: part for key, part in decision.items() if not key.endswith("_ref")
            }
        if isinstance(row.get("lane_breakdown"), list):
            item["lane_breakdown"] = [dict(lane) for lane in row["lane_breakdown"]]
        items.append(item)
    # Keep the immutable report identity on the same persisted operation event as
    # its safe analysis projection.  Consumers can now prove that chat detail,
    # paginated report data and exports belong to one report without inspecting
    # private payload storage.
    return {
        "schema_version": "supply-chain.report-batch-results.v2",
        "report_run_id": validated["report_run_id"],
        "batch_id": validated["batch_id"],
        "item_offset": validated["item_offset"],
        "items": items,
    }
