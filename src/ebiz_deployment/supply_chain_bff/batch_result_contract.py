"""Fail-closed validation for Level 2 batch outputs and restricted result artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker


class BatchResultContractError(ValueError):
    """A Runtime output or its controlled result artifact violates the frozen contract."""


_PACKAGED_SCHEMA = (
    Path(__file__).parent / "contracts" / "level2" / ("report-batch-results.v1.schema.json")
)
_SOURCE_SCHEMA = (
    Path(__file__).parents[3]
    / "contracts"
    / "supply-chain-level2"
    / ("report-batch-results.v1.schema.json")
)
_SCHEMA = _PACKAGED_SCHEMA if _PACKAGED_SCHEMA.is_file() else _SOURCE_SCHEMA


@lru_cache(maxsize=1)
def _artifact_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise BatchResultContractError("Report result schema is unavailable")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validated_batch_output(
    snapshot: Mapping[str, Any],
    *,
    expected_item_count: int,
) -> dict[str, Any]:
    outputs = snapshot.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != {"result"}:
        raise BatchResultContractError("Runtime batch output is unavailable")
    result = outputs.get("result")
    if isinstance(result, dict) and set(result) == {
        "tenant_id",
        "status",
        "scope",
        "payload",
        "evidence",
        "issues",
    }:
        if result.get("status") != "COMPLETE" or result.get("issues") != []:
            raise BatchResultContractError("Runtime batch returned a blocked business result")
        result = result.get("payload")
    if not isinstance(result, dict):
        raise BatchResultContractError("Runtime batch output is unavailable")
    common = {
        "item_count",
        "complete_count",
        "blocked_count",
        "failed_count",
        "summary_artifact_ref",
        "risk_flags",
    }
    external = {"result_artifact_ref", "result_artifact_hash"}
    inline = {"result_artifact"}
    if set(result) not in {frozenset(common | external), frozenset(common | inline)}:
        raise BatchResultContractError("Runtime batch output fields are invalid")
    result_ref = result.get("result_artifact_ref")
    result_hash = result.get("result_artifact_hash")
    counts = tuple(
        result.get(name)
        for name in ("item_count", "complete_count", "blocked_count", "failed_count")
    )
    if external.issubset(result):
        if not isinstance(result_ref, str) or not 1 <= len(result_ref) <= 512:
            raise BatchResultContractError("Runtime batch result reference is invalid")
        if (
            not isinstance(result_hash, str)
            or len(result_hash) != 64
            or any(character not in "0123456789abcdef" for character in result_hash)
        ):
            raise BatchResultContractError("Runtime batch result hash is invalid")
    elif not isinstance(result.get("result_artifact"), dict):
        raise BatchResultContractError("Runtime inline batch result artifact is invalid")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        raise BatchResultContractError("Runtime batch result counts are invalid")
    item_count, complete_count, blocked_count, failed_count = cast(
        tuple[int, int, int, int], counts
    )
    if item_count != expected_item_count or not 1 <= item_count <= 200:
        raise BatchResultContractError("Runtime batch item count does not match the frozen batch")
    if complete_count + blocked_count + failed_count != item_count:
        raise BatchResultContractError("Runtime batch result counts do not add up")
    summary_ref = result.get("summary_artifact_ref")
    if summary_ref is not None and (
        not isinstance(summary_ref, str) or not 1 <= len(summary_ref) <= 512
    ):
        raise BatchResultContractError("Runtime batch summary reference is invalid")
    risks = result.get("risk_flags")
    if not isinstance(risks, list) or any(not isinstance(risk, str) for risk in risks):
        raise BatchResultContractError("Runtime batch risk flags are invalid")
    return cast(dict[str, Any], result)


def validated_batch_artifact(
    payload: Mapping[str, Any],
    *,
    report_run_id: UUID,
    batch_id: UUID,
    item_offset: int,
    expected_item_count: int,
    expected_counts: tuple[int, int, int],
) -> dict[str, Any]:
    error = next(_artifact_validator().iter_errors(payload), None)
    if error is not None:
        raise BatchResultContractError("Report result artifact violates its frozen schema")
    if payload.get("report_run_id") != str(report_run_id) or payload.get("batch_id") != str(
        batch_id
    ):
        raise BatchResultContractError("Report result artifact identity does not match its batch")
    if payload.get("item_offset") != item_offset:
        raise BatchResultContractError("Report result artifact offset does not match its batch")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != expected_item_count:
        raise BatchResultContractError("Report result artifact count does not match its batch")
    statuses = [item.get("result_status") for item in items if isinstance(item, Mapping)]
    actual_counts = (
        statuses.count("COMPLETE"),
        statuses.count("BLOCKED"),
        statuses.count("FAILED"),
    )
    if actual_counts != expected_counts:
        raise BatchResultContractError(
            "Report result artifact status counts do not match Runtime output"
        )
    ordinals = [item.get("ordinal") for item in items if isinstance(item, Mapping)]
    if ordinals != list(range(item_offset, item_offset + expected_item_count)):
        raise BatchResultContractError("Report result artifact ordinals are not contiguous")
    return cast(dict[str, Any], payload)


__all__ = [
    "BatchResultContractError",
    "validated_batch_artifact",
    "validated_batch_output",
]
