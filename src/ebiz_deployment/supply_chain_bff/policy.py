"""Immutable Supply Chain policy validation and canonical digesting."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

_PACKAGED_SCHEMA = Path(__file__).parent / "contracts" / "level2" / "policy.v1.schema.json"
_SOURCE_SCHEMA = (
    Path(__file__).parents[3] / "contracts" / "supply-chain-level2" / "policy.v1.schema.json"
)
_SCHEMA = _PACKAGED_SCHEMA if _PACKAGED_SCHEMA.is_file() else _SOURCE_SCHEMA


class PolicyInvalid(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(errors[0] if errors else "policy is invalid")
        self.errors = errors


def default_policy_document() -> dict[str, Any]:
    """Return the immutable product fallback used when a tenant has no active policy."""
    return {
        "schema_version": "supply-chain.policy.v1",
        "name": "Built-in AUTO/FBM fallback",
        "description": (
            "Read/preview-only fallback. AUTO resolves to FBM and missing capital cost "
            "uses the documented monthly 1% planning assumption."
        ),
        "defaults": {
            "fulfillment": "AUTO",
            "mixed_ratio": None,
            "production_days": None,
            "factory_to_sellable_days_fba": None,
            "factory_to_sellable_days_fbm": None,
            "moq": None,
            "order_multiple": None,
            "monthly_capital_cost_rate": None,
            "monthly_storage_cost_fba": None,
            "monthly_storage_cost_fbm": None,
            "hold": None,
        },
        "sku_rules": [],
    }


def validate_policy(document: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    errors = sorted(
        (_format_error(error) for error in validator.iter_errors(document)),
        key=str,
    )
    warnings: list[str] = []
    for path, rule in _rules(document):
        ratio = rule.get("mixed_ratio")
        if isinstance(ratio, dict):
            if abs(float(ratio.get("fba", 0)) + float(ratio.get("fbm", 0)) - 1.0) > 1e-9:
                errors.append(f"{path}.mixed_ratio: fba and fbm must sum to 1")
        hold = rule.get("hold")
        if isinstance(hold, dict):
            try:
                effective = _timestamp(hold["effective_at"])
                review = _timestamp(hold["review_at"])
                expires = _timestamp(hold["expires_at"])
                if not effective <= review <= expires:
                    errors.append(f"{path}.hold: require effective_at <= review_at <= expires_at")
            except (KeyError, TypeError, ValueError):
                pass
        if rule.get("monthly_capital_cost_rate") is None:
            warnings.append(f"{path}: monthly capital cost defaults to 1%")
        if (
            rule.get("monthly_storage_cost_fba") is None
            or rule.get("monthly_storage_cost_fbm") is None
        ):
            warnings.append(f"{path}: storage cost is incomplete")
    if errors:
        raise PolicyInvalid(errors)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), tuple(dict.fromkeys(warnings))


def _rules(document: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    defaults = document.get("defaults")
    if isinstance(defaults, dict):
        found.append(("defaults", defaults))
    sku_rules = document.get("sku_rules")
    if isinstance(sku_rules, list):
        found.extend(
            (f"sku_rules[{index}]", rule)
            for index, rule in enumerate(sku_rules)
            if isinstance(rule, dict)
        )
    return found


def _timestamp(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("timestamp must be a string")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must carry timezone")
    return value


def _format_error(error: jsonschema.ValidationError) -> str:
    path = ".".join(str(part) for part in error.absolute_path) or "$"
    return f"{path}: {error.message}"
