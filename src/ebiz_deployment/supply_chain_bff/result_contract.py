"""Fail-closed validation and safe projection of Supply Chain public results."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator


class OutputContractError(ValueError):
    """The Runtime terminal snapshot does not contain a valid public result."""


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    distribution = metadata.distribution("ebiz-agent-inventory-supply-chain")
    schema_path = Path(
        str(distribution.locate_file("inventory_supply_chain_agent/schemas/result.schema.yaml"))
    )
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise OutputContractError("Supply Chain result schema is unavailable")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validated_result(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    outputs = snapshot.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != {"result"}:
        raise OutputContractError("Runtime terminal result is unavailable")
    result = outputs.get("result")
    if not isinstance(result, dict):
        raise OutputContractError("Runtime terminal result is unavailable")
    if next(_validator().iter_errors(result), None) is not None:
        raise OutputContractError("Runtime terminal result violates its Output Schema")
    return cast(dict[str, Any], result)


def public_result(result: Mapping[str, Any]) -> dict[str, object]:
    """Project only fields the UI contract allows; evidence and internals stay server-side."""

    scope = _mapping(result.get("scope"))
    business_issues = [
        {
            "code": issue.get("code"),
            "message": issue.get("message"),
            "blocking": issue.get("blocking"),
        }
        for value in _sequence(result.get("issues"))
        if (issue := _mapping(value))
    ]
    payload = _mapping(result.get("payload"))
    forecast = _mapping(payload.get("forecast"))
    classification = _mapping(payload.get("classification"))
    route = _mapping(payload.get("route"))
    replenishment = _mapping(payload.get("replenishment"))
    analysis = _mapping(payload.get("analysis"))
    return {
        "schema_version": "supply-chain.item-result.v1",
        "sku": scope.get("sku"),
        "lane_id": "FBM",
        "fulfillment_source": "ERP",
        "quadrant": classification.get("quadrant"),
        "route": route.get("action"),
        "available_quantity": route.get("inventory_position"),
        "mean_daily_sales": forecast.get("mean_daily_demand"),
        "gross_margin_ratio": None,
        "coverage_days": route.get("projected_coverage_days"),
        "downstream_inbound_days": None,
        "preview_quantity": replenishment.get("final_quantity"),
        "analysis": analysis.get("explanation"),
        "result_status": result.get("status"),
        "business_issues": business_issues,
    }


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


__all__ = ["OutputContractError", "public_result", "validated_result"]
