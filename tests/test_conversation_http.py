import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Header
from jsonschema import Draft202012Validator

from ebiz_deployment.supply_chain_bff.conversation_api import conversation_router
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conflict", "expected", "retryable"),
    [
        ("CONVERSATION_BUSY", "CONVERSATION_BUSY", True),
        ("REQUEST_CONTENT_CONFLICT", "REQUEST_CONTENT_CONFLICT", False),
        ("unexpected-internal-detail", "BFF_HTTP_409", False),
    ],
)
async def test_conversation_conflicts_survive_application_error_handler(
    conflict, expected, retryable
):
    from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
    from ebiz_deployment.supply_chain_bff.config import BffSettings
    from ebiz_deployment.supply_chain_bff.eta import EtaProfile
    from ebiz_deployment.supply_chain_bff.level2_repository import ResourceConflict

    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        eta_profile=EtaProfile(
            version="test",
            fixed_seconds=1,
            per_item_seconds=1,
            concurrency=4,
            uncertainty_ratio=0.25,
        ),
    )
    signer = CursorSigner(b"x" * 32, ttl=timedelta(seconds=60))
    app = create_app(BffContainer(settings, object(), object(), object(), signer))
    app.include_router(
        conversation_router(
            SimpleNamespace(submit=AsyncMock(side_effect=ResourceConflict(conflict))),
            SimpleNamespace(
                openclaw_enabled=True, openclaw_tenant_id="tenant-a", openclaw_principal_id="alice"
            ),
            signer,
            lambda: SimpleNamespace(tenant_id="tenant-a", principal_id="alice"),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/supply-chain/v2/openclaw/conversations/{uuid4()}/turns",
            json={"prompt": "hello", "client_request_id": str(uuid4())},
            headers={"X-Request-ID": "request-test-1"},
        )
    assert response.status_code == 409
    assert response.json()["error_code"] == expected
    assert response.json()["retryable"] is retryable
    assert response.json()["request_id"] == "request-test-1"
    assert "unexpected-internal-detail" not in response.text


@pytest.mark.asyncio
async def test_async_contract_sse_and_identity_cursor_boundaries():
    cid, tid = uuid4(), uuid4()
    report_id, batch_id = uuid4(), uuid4()
    now = [datetime(2026, 9, 12, tzinfo=UTC)]
    snapshot = dict(
        turn_id=str(tid),
        conversation_id=str(cid),
        client_request_id=str(uuid4()),
        message_id=str(tid),
        state="completed",
        sequence=2,
        prompt="hello",
        reply="Hello",
        operations=[
            {
                "operation_id": "operation-g12",
                "state": "completed",
                "execution_id": "execution-g12",
            }
        ],
        analyses=[
            {
                "operation_id": "operation-g12",
                "schema_version": "supply-chain.report-batch-results.v2",
                "report_run_id": str(report_id),
                "batch_id": str(batch_id),
                "item_offset": 0,
                "items": [],
            }
        ],
        error=None,
    )
    repo = SimpleNamespace(
        create_conversation=AsyncMock(
            return_value={"conversation_id": str(cid), "profile_version": "v1"}
        ),
        submit=AsyncMock(return_value=tid),
        snapshot=AsyncMock(return_value=snapshot),
        position=AsyncMock(return_value=("completed", 2)),
        events=AsyncMock(
            return_value=[
                {
                    "sequence": 1,
                    "message_id": str(tid),
                    "kind": "assistant.text.delta",
                    "payload": {"text": "Hello"},
                },
                {
                    "sequence": 2,
                    "message_id": str(tid),
                    "kind": "turn.completed",
                    "payload": {"run_id": str(tid)},
                },
            ]
        ),
    )

    def authenticate(
        x_tenant: str = Header(default="tenant-a"), x_user: str = Header(default="alice")
    ):
        return SimpleNamespace(tenant_id=x_tenant, principal_id=x_user)

    app = FastAPI()
    signer = CursorSigner(b"x" * 32, ttl=timedelta(seconds=60), clock=lambda: now[0])
    app.include_router(
        conversation_router(
            repo,
            SimpleNamespace(
                openclaw_enabled=True, openclaw_tenant_id="tenant-a", openclaw_principal_id="alice"
            ),
            signer,
            authenticate,
        )
    )
    base = "/api/supply-chain/v2/openclaw"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(base + "/conversations")).status_code == 201
        response = await client.post(
            f"{base}/conversations/{cid}/turns",
            json={"prompt": "hello", "client_request_id": str(uuid4())},
        )
        assert response.status_code == 202
        assert response.json()["turn_id"] == str(tid)
        blank = await client.post(
            f"{base}/conversations/{cid}/turns",
            json={"prompt": "   ", "client_request_id": str(uuid4())},
        )
        assert blank.status_code == 422
        assert repo.submit.call_count == 1
        for headers in [{"x_user": "bob"}, {"x_tenant": "tenant-b"}]:
            # HTTP header spelling follows FastAPI's underscore-to-hyphen rule.
            headers = {k.replace("_", "-"): v for k, v in headers.items()}
            assert (await client.get(f"{base}/turns/{tid}", headers=headers)).status_code == 403
        restored = (await client.get(f"{base}/turns/{tid}")).json()
        assert restored["error"] is None
        assert restored["operations"][0]["operation_id"] == "operation-g12"
        assert restored["analyses"][0]["operation_id"] == "operation-g12"
        assert restored["analyses"][0]["report_run_id"] == str(report_id)
        assert restored["analyses"][0]["batch_id"] == str(batch_id)
        cursor = restored["cursor"]
        stream = await client.get(f"{base}/turns/{tid}/events")
        assert stream.status_code == 200
        assert stream.text.count("event: activity") == 2
        assert "Hello" in stream.text and "turn.completed" in stream.text
        # Published docs must describe the actual stream, including its JSON data frames.
        specification = (await client.get("/openapi.json")).json()
        content = specification["paths"][f"{base}/turns/{{tid}}/events"]["get"]["responses"]["200"][
            "content"
        ]
        assert set(content) == {"text/event-stream"}
        assert content["text/event-stream"]["schema"]["type"] == "string"
        event_schema = content["text/event-stream"]["x-sse-data-schema"]
        validator = Draft202012Validator(event_schema)
        for line in stream.text.splitlines():
            if line.startswith("data: "):
                validator.validate(json.loads(line.removeprefix("data: ")))
        assert not validator.is_valid(
            {"sequence": 0, "kind": "unknown", "message_id": "m", "payload": {}}
        )
        assert (
            await client.get(
                f"{base}/turns/{tid}/events", headers={"Last-Event-ID": cursor + "tampered"}
            )
        ).status_code == 400
        now[0] += timedelta(seconds=61)
        assert (
            await client.get(f"{base}/turns/{tid}/events", headers={"Last-Event-ID": cursor})
        ).status_code == 410
