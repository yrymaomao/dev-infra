from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import yaml

from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner

ROOT = Path(__file__).parents[1] / "contracts" / "supply-chain-level2"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _git_text_sha256(path: Path) -> str:
    """Hash the repository text bytes independent of checkout line endings."""

    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode()).hexdigest()


def test_contract_schemas_are_draft_2020_12_valid() -> None:
    paths = [
        ROOT / "mcp-tools.schema.json",
        ROOT / "mq" / "report-batch-requested.v1.schema.json",
        ROOT / "policy.v1.schema.json",
        ROOT / "forecast.v1.schema.json",
        ROOT / "report-batch-results.v1.schema.json",
        ROOT / "report-batch-results.v2.schema.json",
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
    assert _git_text_sha256(schema_path) == (
        "81f0c736538e3f841c7bb5145828076af0bfe28197120a247e558f20bcd14267"
    )
    assert _git_text_sha256(fixture_path) == (
        "16abec2ba6b93f815ed7f467597029f60dd9abb896f3997bde302825933f65e3"
    )


def test_agent_v2_batch_result_fixture_validates() -> None:
    schema = _load_json(ROOT / "report-batch-results.v2.schema.json")
    fixture = _load_json(ROOT / "fixtures" / "report-batch-results.v2.valid.json")
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        fixture
    )
    assert fixture["schema_version"] == "supply-chain.report-batch-results.v2"


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
        "/api/supply-chain/v2/reports/{report_run_id}/export.csv",
        "/api/supply-chain/v2/reports/{report_run_id}/cancel",
        "/api/supply-chain/v2/schedules",
        "/api/supply-chain/v2/schedules/{schedule_id}",
        "/api/supply-chain/v2/schedules/{schedule_id}/run-now",
        "/api/supply-chain/v2/schedules/dispatch-due",
        "/api/supply-chain/v2/policies/validate",
        "/api/supply-chain/v2/policies",
        "/api/supply-chain/v2/policies/{version}",
        "/api/supply-chain/v2/policies/{version}/activate",
    }
    assert expected <= set(paths)
    report = document["components"]["schemas"]["ReportSnapshot"]
    assert report["properties"]["schema_version"]["enum"] == [
        "supply-chain.report.v1",
        "supply-chain.report.v2",
    ]
    assert set(report["oneOf"][1]["required"]) == {
        "selection_snapshot",
        "policy_snapshot",
        "external_signals",
        "model_versions",
    }
    cancel_response = paths["/api/supply-chain/v2/reports/{report_run_id}/cancel"]["post"][
        "responses"
    ]["202"]["content"]["application/json"]["schema"]
    assert cancel_response == {"$ref": "#/components/schemas/ReportCancelAccepted"}
    assert (
        document["components"]["schemas"]["ReportCancelAccepted"]["properties"]["semantics"][
            "const"
        ]
        == "BEST_EFFORT"
    )


def test_runtime_app_and_frozen_openapi_have_the_same_level2_paths() -> None:
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        level2_enabled=True,
        openclaw_enabled=True,
        openclaw_reception_enabled=True,
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            level2_repository=SimpleNamespace(
                _factory=object(),
                _payload_store=object(),
            ),  # type: ignore[arg-type]
        )
    )
    generated_document = app.openapi()
    generated = {
        path
        for path in generated_document["paths"]
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
    turn_request = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))[
        "components"
    ]["schemas"]["OpenClawTurnRequest"]
    assert turn_request["properties"]["prompt"]["maxLength"] == 8192
    cancel_schema = generated_document["components"]["schemas"]["ReportCancelAccepted"]
    assert cancel_schema["properties"]["semantics"]["const"] == "BEST_EFFORT"
    assert cancel_schema["properties"]["runtime_completion"]["const"] == "NOT_CONFIRMED"


def test_pure_v6_mode_removes_legacy_analysis_batch_routes() -> None:
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        legacy_batches_enabled=False,
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

    paths = set(app.openapi()["paths"])
    assert not any(path.startswith("/api/supply-chain/v2/analysis-batches") for path in paths)
    assert "/api/supply-chain/v2/report-runs" in paths
