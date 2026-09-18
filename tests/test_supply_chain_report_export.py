from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import httpx
import jwt
import pytest

from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner
from ebiz_deployment.supply_chain_bff.eta import EtaProfile
from ebiz_deployment.supply_chain_bff.report_export import (
    REPORT_EXPORT_COLUMNS,
    report_export_header,
    report_export_row,
)


def test_report_export_projects_only_safe_summary_fields() -> None:
    source = {
        "ordinal": 7,
        "sku": "SKU-7",
        "result_status": "BLOCKED",
        "fulfillment_mode": "MIXED",
        "fulfillment_source": "POLICY",
        "decision": {"selected_action": "HOLD_UNTIL_REVIEW", "selected_discount_rate": 0},
        "business_issues": [
            {"code": "MIXED_RATIO_UNAVAILABLE", "message": "safe", "blocking": True}
        ],
        "runtime_error": None,
        "risk_flags": ["CAPITAL_COST_DEFAULTED_1_PERCENT"],
        "evidence_refs": [{"evidence_ref": "must-not-be-exported"}],
        "raw_payload": "must-not-be-exported",
    }
    content = report_export_header() + report_export_row(source)
    rows = list(csv.reader(io.StringIO(content)))

    assert rows[0] == list(REPORT_EXPORT_COLUMNS)
    assert rows[1] == [
        "7",
        "SKU-7",
        "BLOCKED",
        "MIXED",
        "POLICY",
        "HOLD_UNTIL_REVIEW",
        "0",
        "MIXED_RATIO_UNAVAILABLE",
        "",
        "CAPITAL_COST_DEFAULTED_1_PERCENT",
    ]
    assert "must-not-be-exported" not in content


def test_report_export_neutralizes_spreadsheet_formula_prefixes() -> None:
    content = report_export_row(
        {
            "ordinal": 0,
            "sku": '=HYPERLINK("unsafe")',
            "result_status": "FAILED",
            "runtime_error": {"error_code": "+SUM(1,1)"},
            "risk_flags": [],
            "business_issues": [],
        }
    )
    row = next(csv.reader(io.StringIO(content)))
    assert row[1].startswith("'=")
    assert row[8].startswith("'+")


@pytest.mark.asyncio
async def test_report_export_streams_all_terminal_pages_and_is_tenant_scoped() -> None:
    report_id = UUID("41eec3b4-e49e-4268-b8cb-4ccce48d2d8f")
    requested_offsets: list[int] = []
    items = [
        {
            "ordinal": index,
            "sku": f"FX-EXPORT-{index:03d}",
            "result_status": ("COMPLETE", "BLOCKED", "FAILED")[index % 3],
            "fulfillment_mode": "FBM",
            "fulfillment_source": "DEFAULT_FBM",
            "decision": {"selected_action": "KEEP", "selected_discount_rate": 0}
            if index % 3 == 0
            else None,
            "business_issues": [
                {"code": "SKU_NOT_FOUND", "message": "No identity", "blocking": True}
            ]
            if index % 3 == 1
            else [],
            "runtime_error": {
                "error_code": "PROVIDER_UNAVAILABLE",
                "safe_message": "Provider unavailable",
            }
            if index % 3 == 2
            else None,
            "risk_flags": [],
            "raw_payload": "FX-MUST-NOT-BE-EXPORTED",
        }
        for index in range(201)
    ]

    class Repository:
        async def get_report(self, **kwargs: object) -> dict[str, object] | None:
            assert kwargs["tenant_id"] == "tenant-a"
            assert kwargs["report_run_id"] == report_id
            offset = int(kwargs["item_offset"])  # type: ignore[arg-type]
            assert kwargs["item_limit"] == 200
            requested_offsets.append(offset)
            return {
                "status": "PARTIAL",
                "items": items[offset : offset + 200],
                "next_item_offset": 200 if offset == 0 else None,
            }

    secret = "j" * 32
    settings = BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret=secret,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        level2_enabled=True,
        eta_profile=EtaProfile(
            version="test",
            fixed_seconds=1,
            per_item_seconds=1,
            concurrency=1,
            uncertainty_ratio=0.1,
        ),
    )
    app = create_app(
        BffContainer(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=7)),
            level2_repository=Repository(),  # type: ignore[arg-type]
        )
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        response = await client.get(f"/api/supply-chain/v2/reports/{report_id}/export.csv")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].endswith(f'supply-chain-{report_id}.csv"')
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == list(REPORT_EXPORT_COLUMNS)
    assert requested_offsets == [0, 200]
    assert len(rows) == 202
    assert [row[0] for row in rows[1:]] == [str(index) for index in range(201)]
    assert [row[1] for row in rows[1:]] == [item["sku"] for item in items]
    assert [row[2] for row in rows[1:]] == [item["result_status"] for item in items]
    for index, row in enumerate(rows[1:]):
        assert row[7] == ("SKU_NOT_FOUND" if index % 3 == 1 else "")
        assert row[8] == ("PROVIDER_UNAVAILABLE" if index % 3 == 2 else "")
    assert "FX-MUST-NOT-BE-EXPORTED" not in response.text
    assert token not in response.text
    if destination := os.environ.get("SUPPLY_CHAIN_EXPORT_EVIDENCE_DIR"):
        evidence = Path(destination)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "export.csv").write_bytes(response.content)
        (evidence / "fixture.json").write_text(
            json.dumps(
                {
                    "kind": "SYNTHETIC_REPOSITORY_ROWS_NOT_FULL_REPORT_SCHEMA",
                    "report_id": str(report_id),
                    "items": items,
                }
            ),
            encoding="utf-8",
        )
