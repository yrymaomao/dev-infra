from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import jsonschema
import yaml

from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner

ROOT = Path(__file__).parents[1] / "contracts" / "supply-chain-level2"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_contract_schemas_are_draft_2020_12_valid() -> None:
    paths = [
        ROOT / "mcp-tools.schema.json",
        ROOT / "mq" / "report-batch-requested.v1.schema.json",
        ROOT / "policy.v1.schema.json",
        ROOT / "forecast.v1.schema.json",
        ROOT / "report-batch-results.v1.schema.json",
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


def test_agent_batch_result_fixture_is_frozen_and_validates() -> None:
    schema_path = ROOT / "report-batch-results.v1.schema.json"
    fixture_path = ROOT / "fixtures" / "report-batch-results.valid.json"
    schema = _load_json(schema_path)
    fixture = _load_json(fixture_path)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        fixture
    )
    assert hashlib.sha256(schema_path.read_bytes()).hexdigest() == (
        "81f0c736538e3f841c7bb5145828076af0bfe28197120a247e558f20bcd14267"
    )
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == (
        "16abec2ba6b93f815ed7f467597029f60dd9abb896f3997bde302825933f65e3"
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
        "/api/supply-chain/v2/reports",
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


def test_runtime_app_and_frozen_openapi_have_the_same_level2_paths() -> None:
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        level2_enabled=True,
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            level2_repository=object(),  # type: ignore[arg-type]
        )
    )
    generated = {
        path
        for path in app.openapi()["paths"]
        if path.startswith("/api/supply-chain/v2/")
        and not path.startswith("/api/supply-chain/v2/analysis-batches")
        and path
        not in {
            "/api/supply-chain/v2/runtime-profile",
            "/api/supply-chain/v2/submission-feedback",
        }
    }
    frozen = set(yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))["paths"])
    assert generated == frozen
