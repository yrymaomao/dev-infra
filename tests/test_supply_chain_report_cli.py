from __future__ import annotations

import argparse
from uuid import UUID

import httpx

from ebiz_deployment.supply_chain_bff.report_cli import _run


def test_dispatch_due_calls_only_the_tenant_scoped_bff_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202,
            json={
                "schema_version": "supply-chain.schedule-dispatch.v1",
                "accepted": True,
                "created_report_count": 1,
            },
        )

    with httpx.Client(
        base_url="http://127.0.0.1:8100",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = _run(argparse.Namespace(area="schedule", action="dispatch-due"), client)

    assert result["created_report_count"] == 1
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/api/supply-chain/v2/schedules/dispatch-due")
    ]


def test_run_now_forwards_the_stable_request_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Idempotency-Key"] == "weekly-2026-09-12"
        return httpx.Response(
            202,
            json={
                "schema_version": "supply-chain.schedule-run-accepted.v1",
                "report_run_id": "00000000-0000-4000-8000-000000000002",
                "status": "SELECTING",
            },
        )

    with httpx.Client(
        base_url="http://127.0.0.1:8100",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = _run(
            argparse.Namespace(
                area="schedule",
                action="run-now",
                schedule_id=UUID("00000000-0000-4000-8000-000000000001"),
                request_id="weekly-2026-09-12",
            ),
            client,
        )

    assert result["status"] == "SELECTING"
