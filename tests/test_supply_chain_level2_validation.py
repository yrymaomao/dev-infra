from __future__ import annotations

import json
import time as clock_time
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from pydantic import ValidationError

from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner
from ebiz_deployment.supply_chain_bff.level2_contracts import (
    InventorySelector,
    OnDemandContextRequest,
    ReportRunRequest,
    ScheduleCreate,
    SchedulePatch,
    SelectionPreviewRequest,
)
from ebiz_deployment.supply_chain_bff.level2_repository import next_weekly_fire
from ebiz_deployment.supply_chain_bff.policy import PolicyInvalid, validate_policy
from ebiz_deployment.supply_chain_bff.selection_csv import (
    CSV_TEMPLATE_BYTES,
    CSV_TEMPLATE_VERSION,
    CsvFileError,
    parse_selection_csv,
)

ROOT = Path(__file__).parents[1]


class _IdleCoordinator:
    async def run_forever(self, *, stop: object) -> None:
        return None


class _ScheduleRepository:
    async def create_schedule(
        self,
        *,
        tenant_id: str,
        request: ScheduleCreate,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, object]:
        assert tenant_id == "tenant-a"
        assert idempotency_key == "schedule-create-stable-1"
        assert request.local_time.isoformat() == "12:00:00"
        assert request.fixed_skus == ()
        return {"schedule_id": "00000000-0000-4000-8000-000000000001"}

    async def enqueue_due_schedules(self, *, tenant_id: str, now: datetime) -> int:
        assert tenant_id == "tenant-a"
        assert now.tzinfo is not None
        return 2


class _ScheduleWorker:
    async def run_forever(self, *, stop: object) -> None:
        return None


#: The Supply Chain profile only answers for session keys its own agent instance minted.
SESSION_KEY = "agent:main:openclaw:" + "0" * 48


class _OpenClawRepository:
    def __init__(self) -> None:
        self.ended = False

    async def exchange_openclaw_run(
        self,
        *,
        selector: str,
        run_id: str,
        tenant_id: str,
        principal_id: str,
        agent_id: str,
        now: datetime,
        model_session_id: str | None = None,
        turn_id: object | None = None,
    ) -> dict[str, str]:
        assert selector == "supply-chain-dev"
        assert run_id == "run-a"
        assert agent_id == "main"
        assert model_session_id is None
        assert turn_id is None
        assert now.tzinfo is not None
        return {
            "tenantId": tenant_id,
            "principalId": principal_id,
            "sessionId": "00000000-0000-4000-8000-000000000009",
            "sessionKey": SESSION_KEY,
            "runId": run_id,
        }

    async def authorize_openclaw_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        session_key: str,
        now: datetime,
    ) -> tuple[bool, str]:
        assert (tenant_id, principal_id, session_key) == (
            "tenant-local-dev",
            "openclaw-supply-chain",
            SESSION_KEY,
        )
        assert now.tzinfo is not None
        return True, "3"

    async def end_openclaw_run(self, *, run_id: str, now: datetime) -> bool:
        assert run_id == "run-a"
        assert now.tzinfo is not None
        self.ended = True
        return True


def test_selection_request_requires_exactly_one_source() -> None:
    with pytest.raises(ValidationError):
        SelectionPreviewRequest()
    with pytest.raises(ValidationError):
        SelectionPreviewRequest(natural_language="inventory above 20", selector=InventorySelector())
    structured = SelectionPreviewRequest(selector=InventorySelector())
    assert structured.selector is not None
    assert structured.selector.threshold == 20


def test_on_demand_context_accepts_json_array_as_ordered_immutable_skus() -> None:
    request = OnDemandContextRequest.model_validate_json(
        '{"skus":["SKU-1","SKU-2"],"invocation_key":"openclaw-call-1"}'
    )
    assert request.skus == ("SKU-1", "SKU-2")


def test_csv_accepts_valid_rows_deduplicates_and_isolates_conflicts() -> None:
    parsed = parse_selection_csv(
        b"sku,fulfillment_mode,fba_ratio,fbm_ratio\n"
        b"SKU-1,AUTO,,\n"
        b"SKU-1,AUTO,,\n"
        b"SKU-2,MIXED,0.6,0.4\n"
        b"SKU-2,MIXED,0.7,0.3\n"
        b"BAD SKU,FBM,,\n"
        b"SKU-3,FBM,,\n"
    )
    assert [row.sku for row in parsed.rows] == ["SKU-1", "SKU-3"]
    assert [error.code for error in parsed.errors] == [
        "CSV_DUPLICATE_CONFLICT",
        "CSV_DUPLICATE_CONFLICT",
        "CSV_ROW_INVALID",
    ]
    assert parsed.input_row_count == 6


def test_csv_non_numeric_ratio_is_a_row_error_not_a_file_error() -> None:
    parsed = parse_selection_csv(
        b"sku,fulfillment_mode,fba_ratio,fbm_ratio\nSKU-BAD,MIXED,nope,0.5\nSKU-GOOD,FBM,,\n"
    )
    assert [row.sku for row in parsed.rows] == ["SKU-GOOD"]
    assert [(error.row, error.code) for error in parsed.errors] == [(2, "CSV_ROW_INVALID")]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"wrong,header\nvalue,one\n",
        "sku,fulfillment_mode,fba_ratio,fbm_ratio\nSKU-1,AUTO,,\n".encode("utf-16"),
    ],
)
def test_csv_file_failures_reject_the_whole_file(content: bytes) -> None:
    with pytest.raises(CsvFileError):
        parse_selection_csv(content)


def test_csv_10001_rows_is_rejected_not_truncated() -> None:
    rows = ["sku,fulfillment_mode,fba_ratio,fbm_ratio"]
    rows.extend(f"SKU-{index},AUTO,," for index in range(10_001))
    with pytest.raises(CsvFileError, match="10000"):
        parse_selection_csv(("\n".join(rows) + "\n").encode())


def test_csv_file_size_is_bounded_before_parsing() -> None:
    with pytest.raises(CsvFileError, match="5 MiB"):
        parse_selection_csv(b"x" * (5 * 1024 * 1024 + 1))


def test_csv_v1_dialect_accepts_bom_and_quoted_newline_without_column_drift() -> None:
    parsed = parse_selection_csv(
        b"\xef\xbb\xbfsku,fulfillment_mode,fba_ratio,fbm_ratio\n"
        b'"SKU-1",FBM,,\n"SKU-2\nCONTINUED",FBM,,\nSKU-3,FBM,,\n'
    )
    assert [row.sku for row in parsed.rows] == ["SKU-1", "SKU-3"]
    assert [(error.row, error.code) for error in parsed.errors] == [(3, "CSV_ROW_INVALID")]


@pytest.mark.parametrize(
    "content",
    [
        b"sku;fulfillment_mode;fba_ratio;fbm_ratio\nSKU-1;FBM;;\n",
        b"sku,fulfillment_mode,fba_ratio,fbm_ratio,unexpected\nSKU-1,FBM,,,value\n",
        b"sku,fulfillment_mode,fba_ratio\nSKU-1,FBM,\n",
    ],
)
def test_csv_v1_dialect_rejects_unpublished_headers_or_delimiter(content: bytes) -> None:
    with pytest.raises(CsvFileError, match="header"):
        parse_selection_csv(content)


@pytest.mark.anyio
async def test_csv_template_endpoint_publishes_exact_versioned_dialect() -> None:
    secret = "j" * 32
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret=secret,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        level2_enabled=True,
    )
    token = jwt.encode(
        {
            "aud": "agent-runtime",
            "sub": "user-a",
            "tenant_id": "tenant-a",
            "exp": int(clock_time.time()) + 60,
        },
        secret,
        algorithm="HS256",
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=_IdleCoordinator(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            level2_repository=object(),  # type: ignore[arg-type]
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        response = await client.get("/api/supply-chain/v2/selection-imports/template.csv")

    assert response.status_code == 200
    assert response.content == CSV_TEMPLATE_BYTES
    assert response.headers["x-supply-chain-template-version"] == CSV_TEMPLATE_VERSION
    assert response.headers["content-disposition"].endswith('supply-chain-selection-v1.csv"')


def test_schedule_defaults_to_monday_noon_and_validates_modes() -> None:
    schedule = ScheduleCreate(
        name="Weekly inventory",
        timezone="America/Los_Angeles",
        selection_mode="DYNAMIC_SELECTOR",
        selector=InventorySelector(),
    )
    assert schedule.weekday == 1
    assert schedule.local_time.isoformat() == "12:00:00"
    with pytest.raises(ValidationError):
        ScheduleCreate(
            name="Invalid",
            timezone="UTC",
            selection_mode="FIXED_SKUS",
            fixed_skus=(),
        )
    with pytest.raises(ValidationError, match="timezone is not recognized"):
        ScheduleCreate(
            name="Invalid timezone",
            timezone="Not/A_Real_Zone",
            selection_mode="DYNAMIC_SELECTOR",
            selector=InventorySelector(),
        )


@pytest.mark.anyio
async def test_schedule_create_accepts_the_frozen_openapi_json_shape() -> None:
    secret = "j" * 32
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret=secret,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        level2_enabled=True,
    )
    token = jwt.encode(
        {
            "aud": "agent-runtime",
            "sub": "user-a",
            "tenant_id": "tenant-a",
            "exp": int(clock_time.time()) + 60,
        },
        secret,
        algorithm="HS256",
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=_IdleCoordinator(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            level2_repository=_ScheduleRepository(),  # type: ignore[arg-type]
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        response = await client.post(
            "/api/supply-chain/v2/schedules",
            headers={"Idempotency-Key": "schedule-create-stable-1"},
            json={
                "name": "Weekly Supply Chain Review",
                "timezone": "America/Los_Angeles",
                "weekday": 1,
                "local_time": "12:00",
                "selection_mode": "DYNAMIC_SELECTOR",
                "selector": {
                    "quantity_metric": "AVAILABLE_QUANTITY",
                    "operator": "GT",
                    "threshold": 20,
                },
                "fixed_skus": [],
                "policy_mode": "ACTIVE_AT_RUN",
                "policy_version": None,
                "active": True,
            },
        )

    assert response.status_code == 201, response.text


@pytest.mark.anyio
async def test_dispatch_due_is_tenant_scoped_and_returns_created_count() -> None:
    secret = "j" * 32
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret=secret,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        level2_enabled=True,
        level2_mq_enabled=True,
        autonomous_schedule_dispatch_enabled=False,
    )
    token = jwt.encode(
        {
            "aud": "agent-runtime",
            "sub": "scheduler",
            "tenant_id": "tenant-a",
            "exp": int(clock_time.time()) + 60,
        },
        secret,
        algorithm="HS256",
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=_IdleCoordinator(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            level2_repository=_ScheduleRepository(),  # type: ignore[arg-type]
            level2_worker=_ScheduleWorker(),  # type: ignore[arg-type]
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        response = await client.post("/api/supply-chain/v2/schedules/dispatch-due")

    assert response.status_code == 202
    assert response.json() == {
        "schema_version": "supply-chain.schedule-dispatch.v1",
        "accepted": True,
        "created_report_count": 2,
    }


def test_schedule_patch_can_atomically_change_selection_mode() -> None:
    patch = SchedulePatch.model_validate(
        {
            "selection_mode": "FIXED_SKUS",
            "selector": None,
            "fixed_skus": ["SKU-1", "SKU-2"],
        }
    )
    assert patch.selection_mode == "FIXED_SKUS"
    assert patch.selector is None
    assert patch.fixed_skus == ("SKU-1", "SKU-2")


def test_schedule_patch_distinguishes_missing_local_time_from_null() -> None:
    assert SchedulePatch.model_validate({"active": False}).active is False
    assert SchedulePatch.model_validate({"local_time": "23:59"}).local_time == time(23, 59)
    with pytest.raises(ValidationError):
        SchedulePatch.model_validate({"local_time": None})


def test_weekly_fire_uses_tenant_timezone_across_dst() -> None:
    schedule = ScheduleCreate(
        name="Weekly inventory",
        timezone="America/Los_Angeles",
        selection_mode="DYNAMIC_SELECTOR",
        selector=InventorySelector(),
    )
    before_dst = next_weekly_fire(schedule, after=datetime(2026, 3, 7, 12, tzinfo=UTC))
    after_dst = next_weekly_fire(schedule, after=before_dst)
    assert before_dst == datetime(2026, 3, 9, 19, tzinfo=UTC)
    assert after_dst == datetime(2026, 3, 16, 19, tzinfo=UTC)


def test_pinned_report_requires_policy_version() -> None:
    with pytest.raises(ValidationError):
        ReportRunRequest(
            selection_preview_id="00000000-0000-4000-8000-000000000001",
            policy_mode="PINNED",
        )


def test_policy_digest_is_canonical_and_cross_fields_are_checked() -> None:
    fixture = json.loads(
        (ROOT / "contracts" / "supply-chain-level2" / "fixtures" / "policy.valid.json").read_text(
            encoding="utf-8"
        )
    )
    digest, warnings = validate_policy(fixture)
    assert len(digest) == 64
    assert "defaults: monthly capital cost defaults to 1%" in warnings
    assert digest == validate_policy(dict(reversed(list(fixture.items()))))[0]
    fixture["defaults"]["fulfillment"] = "MIXED"
    fixture["defaults"]["mixed_ratio"] = {"fba": 0.8, "fbm": 0.8}
    with pytest.raises(PolicyInvalid, match="sum to 1"):
        validate_policy(fixture)


@pytest.mark.anyio
@pytest.mark.parametrize("crm_enabled", [False, True])
async def test_openclaw_credential_policy_and_endpoints_preserve_trusted_binding(
    crm_enabled: bool,
) -> None:
    connector = "c" * 32
    gateway_key = "g" * 32
    repository = _OpenClawRepository()
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"h" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8013",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        level2_enabled=True,
        openclaw_enabled=True,
        crm_openclaw_enabled=crm_enabled,
        openclaw_connector_credential=connector,
        tool_gateway_jwt_key=gateway_key,
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=_IdleCoordinator(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"h" * 32, ttl=timedelta(days=7)),
            level2_repository=repository,  # type: ignore[arg-type]
            level2_worker=_ScheduleWorker(),  # type: ignore[arg-type]
        )
    )
    headers = {"Authorization": f"Bearer {connector}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff.test", headers=headers
    ) as client:
        credential = await client.post(
            "/api/supply-chain/v2/openclaw/credentials",
            json={"selector": "supply-chain-dev", "runId": "run-a"},
        )
        policy = await client.post(
            "/internal/supply-chain/v2/openclaw/authorize",
            json={
                "tenant_id": "tenant-local-dev",
                "principal_id": "openclaw-supply-chain",
                "session_key": SESSION_KEY,
                "action": "invoke",
                "candidate_offer_ids": [
                    "supply-chain-on-demand",
                    "crm-case-advice",
                    "crm-send",
                    "other",
                ],
            },
        )
        ended = await client.post("/api/supply-chain/v2/openclaw/runs/end", json={"runId": "run-a"})

    assert credential.status_code == 200
    payload = credential.json()
    claims = jwt.decode(
        payload["jwt"], gateway_key, algorithms=["HS256"], audience="ebizhub-tool-gateway"
    )
    assert claims["tenant_id"] == "tenant-local-dev"
    assert claims["session_key"] == SESSION_KEY
    assert payload["identity"]["runId"] == "run-a"
    assert policy.status_code == 200, policy.text
    assert policy.json() == {
        "binding_active": True,
        "policy_revision": "3",
        # R1-06: the Supply Chain profile never authorizes another profile's offer,
        # whether or not the CRM profile is enabled on the same BFF.
        "allowed_offer_ids": ["supply-chain-on-demand"],
    }
    assert ended.json() == {"ended": True}
    assert repository.ended is True
