from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml


ROOT = Path(__file__).parents[1] / "contracts" / "supply-chain-level2"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_contract_schemas_are_draft_2020_12_valid() -> None:
    paths = [
        ROOT / "mcp-tools.schema.json",
        ROOT / "mq" / "report-batch-requested.v1.schema.json",
        ROOT / "policy.v1.schema.json",
        ROOT / "forecast.v1.schema.json",
    ]
    for path in paths:
        jsonschema.Draft202012Validator.check_schema(_load_json(path))


def test_mq_fixture_contains_no_sku_payload_and_validates() -> None:
    schema = _load_json(ROOT / "mq" / "report-batch-requested.v1.schema.json")
    fixture = _load_json(ROOT / "fixtures" / "report-batch-requested.valid.json")
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        fixture
    )
    assert "skus" not in fixture
    assert "sql" not in fixture


def test_policy_fixture_validates() -> None:
    schema = _load_json(ROOT / "policy.v1.schema.json")
    fixture = _load_json(ROOT / "fixtures" / "policy.valid.json")
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        fixture
    )


def test_openapi_declares_all_level2_resources() -> None:
    document = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    assert document["openapi"] == "3.1.0"
    paths = document["paths"]
    expected = {
        "/api/supply-chain/v2/selection-previews",
        "/api/supply-chain/v2/selection-previews/{preview_id}",
        "/api/supply-chain/v2/selection-imports",
        "/api/supply-chain/v2/report-runs",
        "/api/supply-chain/v2/reports/{report_run_id}",
        "/api/supply-chain/v2/reports/{report_run_id}/events",
        "/api/supply-chain/v2/reports/{report_run_id}/cancel",
        "/api/supply-chain/v2/schedules",
        "/api/supply-chain/v2/schedules/{schedule_id}",
        "/api/supply-chain/v2/schedules/{schedule_id}/run-now",
        "/api/supply-chain/v2/policies/validate",
        "/api/supply-chain/v2/policies",
        "/api/supply-chain/v2/policies/{version}",
        "/api/supply-chain/v2/policies/{version}/activate",
    }
    assert expected <= set(paths)
