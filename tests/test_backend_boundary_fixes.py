"""Regression cases from BB-B03-201-001 and BB-G05-ACCOUNTING-001."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from reception_fixtures import owner_for, supply_chain

from ebiz_deployment.openclaw_reception.conversation_api import conversation_router
from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner
from ebiz_deployment.supply_chain_bff.eta import EtaProfile
from ebiz_deployment.supply_chain_bff.level2_contracts import OpenClawTurnRequest
from ebiz_deployment.supply_chain_bff.level2_repository import Level2Repository
from ebiz_deployment.supply_chain_bff.selection_csv import parse_selection_csv


@pytest.mark.parametrize("length", [8191, 8192])
def test_openclaw_prompt_character_boundary_is_accepted_without_truncation(length):
    prompt = "a" * length
    request = OpenClawTurnRequest(prompt=prompt, client_request_id=uuid4())
    assert request.prompt == prompt


def test_openclaw_prompt_over_character_boundary_is_rejected_not_truncated():
    with pytest.raises(ValueError):
        OpenClawTurnRequest(prompt="a" * 8193, client_request_id=uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", [" ", ",", "\n", "\t", "，", ";"])
async def test_201_bare_skus_reach_openclaw_reception_unchanged(separator):
    prompt = separator.join(f"Q{i}" for i in range(201))
    response, repository = await submit_prompt(prompt)
    assert response.status_code == 202
    assert repository.submit.await_args.args[-1] == prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        " ".join(f"Q{i}" for i in range(200)),
        " ".join([*(f"Q{i}" for i in range(200)), "Q0"]),
        "Explain inventory demand forecasting and replenishment " * 35,
        "请解释 " + "供应链 库存 预测 补货 " * 55,
        "Explain Q1 versus Q2 for 2026 " * 35,
        "WHAT IS SUPPLY CHAIN " * 60,
        "Q0 q0 SKU-01 sku_02 12345",
    ],
)
async def test_allowed_lists_and_concepts_reach_reception_unchanged(prompt):
    response, repository = await submit_prompt(prompt)
    assert response.status_code == 202
    assert repository.submit.await_args.args[-1] == prompt


async def submit_prompt(prompt, request_id=None):
    # Replace only durable admission: no database, worker or live model is invoked.
    repository = SimpleNamespace(submit=AsyncMock(return_value=uuid4()))
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

    @app.middleware("http")
    async def correlation(request, call_next):
        request.state.request_id = request_id
        return await call_next(request)

    profile = supply_chain(settings)
    app.include_router(
        conversation_router(
            repository,
            profile,
            signer,
            lambda: SimpleNamespace(tenant_id="tenant-a", principal_id="alice"),
            owner_for(profile),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/supply-chain/v2/openclaw/conversations/{uuid4()}/turns",
            json={"prompt": prompt, "client_request_id": str(uuid4())},
        )
    return response, repository


@pytest.mark.parametrize(
    ("rows", "excluded"),
    [
        (["SKU001,FBM,,", "SKU002,FBM,,", "SKU001,FBA,,"], [2, 4]),
        (
            [
                "SKU001,FBM,,",
                "SKU001,FBM,,",
                "SKU002,FBM,,",
                "SKU001,FBA,,",
                "SKU001,FBM,,",
                "SKU001,FBA,,",
            ],
            [2, 3, 5, 6, 7],
        ),
        (["SKU001,MIXED,0.6,0.4", "SKU002,FBM,,", "SKU001,MIXED,0.7,0.3"], [2, 4]),
    ],
)
def test_every_row_of_conflicting_group_is_excluded_once(rows, excluded):
    parsed = parse_selection_csv(
        ("sku,fulfillment_mode,fba_ratio,fbm_ratio\n" + "\n".join(rows)).encode()
    )
    assert [row.sku for row in parsed.rows] == ["SKU002"]
    assert [(error.row, error.code) for error in parsed.errors] == [
        (row, "CSV_DUPLICATE_CONFLICT") for row in excluded
    ]
    assert len(parsed.rows) + len(parsed.errors) == parsed.input_row_count


@pytest.mark.asyncio
async def test_preview_warning_counts_both_excluded_conflict_rows():
    parsed = parse_selection_csv(
        b"sku,fulfillment_mode,fba_ratio,fbm_ratio\nSKU001,FBM,,\nSKU002,FBM,,\nSKU001,FBA,,\n"
    )
    # Exercise the real preview construction; replace storage/transaction I/O only.
    session = MagicMock()
    session.begin.return_value = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    repository = Level2Repository(factory, payload_store=object())
    repository._stage = AsyncMock(
        return_value=SimpleNamespace(payload_ref="payload://test", payload_hash="test")
    )
    repository._commit_staged = AsyncMock()
    await repository.create_csv_preview(
        tenant_id="tenant-a",
        client_request_id="csv-test",
        parsed=parsed,
        now=datetime(2026, 9, 14, tzinfo=UTC),
    )
    preview = session.add.call_args.args[0]
    assert preview.matched_count == 1
    assert preview.warnings == ["2 CSV row(s) were excluded."]
    errors = repository._stage.await_args_list[1].args[1]["errors"]
    assert [error["row"] for error in errors] == [2, 4]
