from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
from agent_runtime.payloads.memory import MemoryPayloadStore
from pydantic import ValidationError
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ebiz_deployment.supply_chain_bff.activity import project_runtime_event
from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.contracts import BatchCreateRequest
from ebiz_deployment.supply_chain_bff.cursor import CursorExpired, CursorInvalid, CursorSigner
from ebiz_deployment.supply_chain_bff.dispatcher import BatchCoordinator
from ebiz_deployment.supply_chain_bff.eta import EtaEstimator, EtaProfile
from ebiz_deployment.supply_chain_bff.models import SCHEMA, Base, BatchItem
from ebiz_deployment.supply_chain_bff.repository import BatchRepository
from ebiz_deployment.supply_chain_bff.result_contract import (
    OutputContractError,
    public_result,
    validated_result,
)
from ebiz_deployment.supply_chain_bff.runtime_client import RuntimeClient, RuntimeRequestError


def profile() -> EtaProfile:
    return EtaProfile(
        version="supply-chain-v5-dev-1",
        fixed_seconds=2.0,
        per_item_seconds=8.0,
        concurrency=4,
        uncertainty_ratio=0.25,
    )


def test_batch_request_normalizes_and_deduplicates_skus() -> None:
    request = BatchCreateRequest(skus=[" 010343937505 ", "010343937505", "010343943674"])
    assert request.skus == ("010343937505", "010343943674")
    assert request.marketplace == "US"
    assert request.fulfillment_mode == "FBM"

    with pytest.raises(ValidationError):
        BatchCreateRequest(skus=[f"sku-{index}" for index in range(201)])


def test_eta_uses_count_then_ewma_after_two_terminal_items() -> None:
    estimator = EtaEstimator(profile())
    initial = estimator.estimate(item_count=20)
    assert initial.low_seconds < initial.high_seconds
    assert initial.profile_version == "supply-chain-v5-dev-1"

    assert estimator.observe(duration_seconds=10, terminal_count=1) is None
    dynamic = estimator.observe(duration_seconds=14, terminal_count=2)
    assert dynamic is not None
    calibrated = estimator.estimate(item_count=20, completed_count=2)
    assert calibrated.dynamic is True
    assert calibrated.low_seconds < calibrated.high_seconds


def test_coordinator_mints_short_lived_tenant_service_token() -> None:
    secret = "j" * 32
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret=secret,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        eta_profile=profile(),
    )
    coordinator = BatchCoordinator(
        repository=object(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        settings=settings,
    )
    authorization = coordinator.authorization_for_tenant("tenant-a")
    claims = jwt.decode(
        authorization.removeprefix("Bearer "),
        secret,
        algorithms=["HS256"],
        audience="agent-runtime",
    )
    assert claims["tenant_id"] == "tenant-a"
    assert claims["actor_type"] == "service"
    assert claims["credential_ref"] == "opaque:runtime-service"
    assert set(claims["scopes"]) == {
        "inventory.read",
        "sales_profit.read",
        "supply_chain.preview",
        "workflow:read",
        "workflow:start",
    }
    assert 0 < claims["exp"] - claims["iat"] <= 300
    feedback_claims = jwt.decode(
        coordinator.authorization_for_tenant(
            "tenant-a",
            include_model_feedback=True,
        ).removeprefix("Bearer "),
        secret,
        algorithms=["HS256"],
        audience="agent-runtime",
    )
    assert set(feedback_claims["scopes"]) == {
        "inventory.read",
        "sales_profit.read",
        "supply_chain.preview",
        "workflow:start",
        "workflow:read",
        "model:feedback",
    }


def test_signed_cursor_binds_batch_tenant_and_sequences() -> None:
    now = datetime(2026, 9, 3, tzinfo=UTC)
    signer = CursorSigner(b"x" * 32, ttl=timedelta(minutes=5), clock=lambda: now)
    token = signer.issue(
        batch_id="batch-1",
        tenant_id="tenant-a",
        sequences={"execution-1": 7, "execution-2": 3},
    )
    decoded = signer.verify(token, batch_id="batch-1", tenant_id="tenant-a")
    assert decoded.sequences == {"execution-1": 7, "execution-2": 3}

    with pytest.raises(CursorInvalid):
        signer.verify(token + "x", batch_id="batch-1", tenant_id="tenant-a")
    with pytest.raises(CursorInvalid):
        signer.verify(token, batch_id="batch-1", tenant_id="tenant-b")

    expired = CursorSigner(
        b"x" * 32,
        ttl=timedelta(minutes=5),
        clock=lambda: now + timedelta(minutes=6),
    )
    with pytest.raises(CursorExpired):
        expired.verify(token, batch_id="batch-1", tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_runtime_client_supports_async_and_015_sync_fallback() -> None:
    responses = iter(
        [
            httpx.Response(
                202,
                json={
                    "execution_id": "execution-1",
                    "session_id": "session-1",
                    "root_execution_id": "execution-1",
                    "status": "CREATED",
                },
            ),
            httpx.Response(
                201,
                json={
                    "execution_id": "execution-2",
                    "root_execution_id": "execution-2",
                    "status": "SUCCEEDED",
                    "outputs": {"result": {"status": "COMPLETE"}},
                },
            ),
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["prefer"] == "respond-async"
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = RuntimeClient(http, base_url="http://127.0.0.1:8000")
        async_result = await client.start(
            authorization="Bearer token",
            payload={"idempotency_key": "one"},
        )
        sync_result = await client.start(
            authorization="Bearer token",
            payload={"idempotency_key": "two"},
        )
    assert async_result.mode == "async"
    assert async_result.session_id == "session-1"
    assert sync_result.mode == "sync-polling"
    assert sync_result.snapshot["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_runtime_client_retries_429_503_and_transport_failures() -> None:
    async def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"retryable": False, "safe_message": "Unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
        client = RuntimeClient(http, base_url="http://127.0.0.1:8000")
        with pytest.raises(RuntimeRequestError) as response_error:
            await client.start(authorization="Bearer token", payload={})
    assert response_error.value.retryable is True
    assert response_error.value.status_code == 503

    async def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as http:
        client = RuntimeClient(http, base_url="http://127.0.0.1:8000")
        with pytest.raises(RuntimeRequestError) as transport_error:
            await client.get_execution(
                authorization="Bearer token",
                execution_id="execution-1",
            )
    assert transport_error.value.retryable is True
    assert transport_error.value.status_code == 503


@pytest.mark.asyncio
async def test_runtime_client_forwards_only_structured_model_feedback() -> None:
    async def feedback(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/model-feedback"
        assert request.headers["authorization"] == "Bearer service-token"
        return httpx.Response(
            200,
            json={
                "title": "Analysis could not be started",
                "summary": "The runtime request failed.",
                "cause": "The provider was unavailable.",
                "next_steps": ["Retry the analysis."],
                "retryable": True,
                "request_id": "26d19d86-b1b6-474c-8760-c4566a6f94da",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(feedback)) as http:
        client = RuntimeClient(http, base_url="http://127.0.0.1:8000")
        response = await client.model_feedback(
            authorization="Bearer service-token",
            payload={"operation": "START_ANALYSIS_BATCH"},
        )
    assert response["retryable"] is True


def test_bff_schema_preserves_v1_tables_and_no_plaintext_sku_column() -> None:
    assert {
        "agent_execution_batch",
        "agent_execution_batch_item",
        "agent_execution_batch_activity",
    } <= {table.name for table in Base.metadata.tables.values()}
    assert {table.schema for table in Base.metadata.tables.values()} == {SCHEMA}
    for table in Base.metadata.tables.values():
        assert "sku" not in table.columns
        assert "skus" not in table.columns
    assert "sku_hash" in Base.metadata.tables[f"{SCHEMA}.agent_execution_batch_item"].columns


def test_activity_projection_never_exposes_runtime_payload_or_unknown_tool() -> None:
    projected = project_runtime_event(
        {
            "execution_id": "26d19d86-b1b6-474c-8760-c4566a6f94da",
            "sequence": 8,
            "event_type": "capability.completed",
            "payload": {
                "node_id": "read_inventory",
                "sql": "select secret",
                "result": {"raw": "private"},
                "duration_ms": 123,
            },
        }
    )
    assert projected.phase == "mcp"
    assert projected.tool_name == "query_inventory_summary_v2"
    assert projected.duration_ms == 123
    assert "sql" not in projected.payload
    assert "result" not in projected.payload

    assert {
        project_runtime_event(
            {
                "execution_id": "26d19d86-b1b6-474c-8760-c4566a6f94da",
                "sequence": sequence,
                "event_type": "capability.started",
                "payload": {"node_id": node_id},
            }
        ).tool_name
        for sequence, node_id in enumerate(
            ("resolve_identity", "read_inventory", "read_sales", "read_cohort"),
            start=10,
        )
    } == {
        "query_sku_upc_mapping",
        "query_inventory_summary_v2",
        "query_sku_sales_profit_windows_v1",
        "query_sku_boston_cohort_v1",
    }

    unknown = project_runtime_event(
        {
            "execution_id": "26d19d86-b1b6-474c-8760-c4566a6f94da",
            "sequence": 9,
            "event_type": "capability.started",
            "payload": {"tool_name": "internal_admin_tool"},
        }
    )
    assert unknown.tool_name is None


def test_terminal_output_schema_is_fail_closed_and_projection_is_safe() -> None:
    result = {
        "tenant_id": "tenant-a",
        "status": "BLOCKED",
        "scope": {"market_scope": "NA_COMPANY", "sku": "010343937505"},
        "payload": None,
        "evidence": [],
        "issues": [
            {
                "code": "DATA_MISSING",
                "message": "Required evidence is missing.",
                "blocking": True,
                "metadata": {},
            }
        ],
    }
    validated = validated_result({"outputs": {"result": result}})
    projected = public_result(validated)
    assert projected["sku"] == "010343937505"
    assert projected["result_status"] == "BLOCKED"
    assert projected["business_issues"] == [
        {
            "code": "DATA_MISSING",
            "message": "Required evidence is missing.",
            "blocking": True,
        }
    ]
    assert "evidence" not in projected
    assert "metadata" not in projected["business_issues"][0]

    with pytest.raises(OutputContractError):
        validated_result({"outputs": {"result": {**result, "unexpected": "field"}}})


@pytest.mark.asyncio
async def test_bff_public_contract_returns_202_profile_and_replayable_sse() -> None:
    batch_id = uuid4()

    class Repository:
        reads = 0
        created_at: datetime | None = None
        created_skus: tuple[str, ...] = ()

        async def create_batch(self, **kwargs: object):
            self.created_at = kwargs["now"]  # type: ignore[assignment]
            request = kwargs["request"]
            assert isinstance(request, BatchCreateRequest)
            self.created_skus = request.skus
            return batch_id

        async def get_batch(self, **_kwargs: object):
            self.reads += 1
            return {
                "schema_version": "supply-chain.analysis-batch.v2",
                "batch_id": str(batch_id),
                "status": "RUNNING" if self.reads == 1 else "SUCCEEDED",
            }

        async def list_activities(self, **_kwargs: object):
            return [
                {
                    "id": str(uuid4()),
                    "stream_sequence": 1,
                    "schema_version": "business-agent.activity-event.v1",
                    "event_type": "batch.accepted",
                    "phase": "validation",
                    "state": "accepted",
                    "message": "Batch accepted.",
                    "execution_id": None,
                    "sequence": None,
                    "tool_name": None,
                    "duration_ms": None,
                    "progress": {"current": 0, "total": 1},
                    "payload": {},
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ]

    class Coordinator:
        async def run_forever(self, *, stop: object) -> None:
            return None

    secret = "j" * 32
    snapshot_override = datetime(2026, 9, 1, tzinfo=UTC)
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret=secret,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        snapshot_time_override=snapshot_override,
        async_start_enabled=True,
        stream_enabled=True,
        activity_ui_enabled=True,
        eta_profile=profile(),
    )
    repository = Repository()
    container = BffContainer(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        coordinator=Coordinator(),  # type: ignore[arg-type]
        cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
    )
    token = jwt.encode(
        {
            "aud": "agent-runtime",
            "sub": "user-a",
            "tenant_id": "tenant-a",
            "exp": int(time.time()) + 60,
        },
        secret,
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)),
        base_url="http://bff.test",
        headers=headers,
    ) as client:
        started_at = time.monotonic()
        accepted = await client.post(
            "/api/supply-chain/v2/analysis-batches",
            json={
                "skus": [
                    "196548430192_M08_128MSDA",
                    "MC_PD100W_charger_Aohai",
                    "010343937505",
                    "abc",
                    "__NO_SUCH_SKU_E2E_20260904__",
                ],
                "marketplace": "US",
                "fulfillment_mode": "FBM",
                "client_request_id": "request-1",
            },
        )
        elapsed = time.monotonic() - started_at
        runtime_profile = await client.get("/api/supply-chain/v2/runtime-profile")
        events = await client.get(f"/api/supply-chain/v2/analysis-batches/{batch_id}/events")
        invalid = await client.post(
            "/api/supply-chain/v2/analysis-batches",
            headers={"X-Request-ID": "request-safe-1"},
            json={"skus": [f"SKU-{index}" for index in range(201)]},
        )
        feedback_disabled = await client.post(
            "/api/supply-chain/v2/submission-feedback",
            json={
                "request_id": "26d19d86-b1b6-474c-8760-c4566a6f94da",
                "locale": "en-US",
                "operation": "START_ANALYSIS_BATCH",
                "facts": {
                    "error_code": "RUNTIME_REQUEST_FAILED",
                    "category": "transient",
                    "phase": "invocation",
                    "retryable": True,
                    "safe_message": "The runtime request failed.",
                },
            },
        )

    assert accepted.status_code == 202, accepted.text
    assert repository.created_at == snapshot_override
    assert repository.created_skus == (
        "196548430192_M08_128MSDA",
        "MC_PD100W_charger_Aohai",
        "010343937505",
        "abc",
        "__NO_SUCH_SKU_E2E_20260904__",
    )
    assert elapsed < 1
    assert accepted.headers["location"].endswith(str(batch_id))
    assert runtime_profile.json()["activity_ui_enabled"] is True
    assert runtime_profile.json()["model_error_polish_enabled"] is False
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: activity" in events.text
    assert "id: " in events.text
    assert invalid.status_code == 422
    assert invalid.json() == {
        "error_code": "BATCH_REQUEST_INVALID",
        "phase": "recovery",
        "category": "validation",
        "retryable": False,
        "safe_message": "The supply chain request is invalid.",
        "request_id": "request-safe-1",
    }
    assert feedback_disabled.status_code == 404
    assert feedback_disabled.json()["safe_message"] == (
        "The requested supply chain resource was not found."
    )


@pytest.mark.asyncio
async def test_persistent_queue_replays_create_and_recovers_expired_lease() -> None:
    database_url = os.environ.get("SUPPLY_CHAIN_BFF_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SUPPLY_CHAIN_BFF_TEST_DATABASE_URL is not configured")
    if not str(make_url(database_url).database).endswith("_test"):
        pytest.fail("refusing destructive queue test outside a *_test database")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE supply_chain_bff.agent_execution_batch_activity, "
                "supply_chain_bff.agent_execution_batch_item, "
                "supply_chain_bff.agent_execution_batch CASCADE"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = MemoryPayloadStore(redacted_fields=frozenset(), inline_classifications=frozenset())
    repository = BatchRepository(
        factory,
        payload_store=store,
        tenant_concurrency=4,
        global_concurrency=32,
        eta_profile=profile(),
    )
    request = BatchCreateRequest(
        skus=[f"0103439375{index:02d}" for index in range(6)],
        client_request_id=f"restart-recovery-{uuid4().hex}",
    )
    tenant_id = f"queue-recovery-{uuid4().hex}"
    now = datetime.now(UTC)
    eta = EtaEstimator(profile()).estimate(item_count=3)
    first = await repository.create_batch(
        tenant_id=tenant_id,
        request=request,
        eta=eta,
        now=now,
    )
    replay = await repository.create_batch(
        tenant_id=tenant_id,
        request=request,
        eta=eta,
        now=now,
    )
    assert replay == first
    created_snapshot = await repository.get_batch(tenant_id=tenant_id, batch_id=first)
    assert created_snapshot is not None
    assert [item["sku"] for item in created_snapshot["items"]] == list(request.skus)
    assert created_snapshot["succeeded_count"] == 0
    assert created_snapshot["blocked_count"] == 0
    assert created_snapshot["failed_count"] == 0
    claimed = await repository.claim_item(worker_id="worker-before-restart", now=now)
    assert claimed is not None
    controlled_payload = await repository.load_payload(claimed)
    assert controlled_payload.skus == tuple(request.skus)
    assert not hasattr(controlled_payload, "authorization")
    async with factory() as session, session.begin():
        await session.execute(
            update(BatchItem)
            .where(BatchItem.id == claimed.item_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )

    restarted = BatchRepository(
        factory,
        payload_store=store,
        tenant_concurrency=4,
        global_concurrency=32,
        eta_profile=profile(),
    )
    reclaimed = await restarted.claim_item(worker_id="worker-after-restart", now=now)
    assert reclaimed is not None
    assert reclaimed.item_id == claimed.item_id
    assert reclaimed.attempt_count == 2
    concurrent = await asyncio.gather(
        *(restarted.claim_item(worker_id=f"worker-{index}", now=now) for index in range(6))
    )
    assert 1 + sum(item is not None for item in concurrent) == 4
    await engine.dispose()
