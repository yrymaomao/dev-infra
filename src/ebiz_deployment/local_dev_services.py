"""Loopback-only deterministic services for ``LOCAL_DEV_E2E``.

These services deliberately exercise the production HTTP and MCP adapters while
remaining isolated from ERP and paid model endpoints.  The credentials below
are fixed development sentinels, not user or production credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException
from mcp.server import MCPServer
from starlette.types import ASGIApp, Receive, Scope, Send

BROKER_AUTH_TOKEN = "local-dev-broker-auth-not-production"
CREDENTIAL_REF = "local-dev-credential-ref"
REQUEST_ACCESS_TOKEN = "local-dev-request-access-not-production"
OPENAI_API_KEY = "local-dev-openai-key-not-production"
LOCAL_TENANT_ID = "tenant-local-dev"

_ALLOWED_CREDENTIAL_PROVIDERS = frozenset({"mcp.streamable_http", "yeaher.erp"})


def _bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="local development authentication required")
    return authorization.removeprefix("Bearer ")


def create_provider_app() -> FastAPI:
    """Create deterministic credential, ERP MCP, health, and model routes."""
    app = FastAPI(title="eBizHub LOCAL_DEV_E2E Providers", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "LOCAL_DEV_E2E"}

    @app.post("/v1/resolve")
    async def resolve_credential(
        body: dict[str, Any] = Body(...), authorization: str | None = Header(default=None)
    ) -> dict[str, str]:
        if _bearer(authorization) != BROKER_AUTH_TOKEN:
            raise HTTPException(status_code=401, detail="invalid local broker authorization")
        provider_id = body.get("provider_id")
        if provider_id not in _ALLOWED_CREDENTIAL_PROVIDERS:
            raise HTTPException(status_code=403, detail="provider is not locally allowlisted")
        if body.get("credential_ref") != CREDENTIAL_REF:
            raise HTTPException(status_code=404, detail="local credential reference not found")
        expires_at = datetime.now(UTC) + timedelta(minutes=5)
        return {
            "provider_id": cast(str, provider_id),
            "access_token": REQUEST_ACCESS_TOKEN,
            "tenant_id": LOCAL_TENANT_ID,
            "tenant_binding_digest": "a" * 64,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }

    @app.post("/v1/responses")
    async def responses(
        body: dict[str, Any] = Body(...), authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        if _bearer(authorization) != OPENAI_API_KEY:
            raise HTTPException(status_code=401, detail="invalid local model authorization")
        model = _required_text(body, "model")
        output = {
            "assessment": "ALIGNED",
            "confidence": 0.96,
            "explanation": "Deterministic local development analysis.",
            "risk_flags": [],
        }
        return {
            "id": "resp_local_dev_001",
            "object": "response",
            "created_at": int(time.time()),
            "model": model,
            "status": "completed",
            "output": [
                {
                    "id": "msg_local_dev_001",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(output, separators=(",", ":")),
                            "annotations": [],
                        }
                    ],
                }
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 64,
                "output_tokens": 32,
                "total_tokens": 96,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }

    return app


class _BearerProtectedApp:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            if headers.get(b"authorization") != f"Bearer {REQUEST_ACCESS_TOKEN}".encode():
                response = HTTPException(status_code=401, detail="invalid local MCP authorization")
                payload = json.dumps({"detail": response.detail}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": response.status_code,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
                return
        await self._app(scope, receive, send)


def create_mcp_app() -> ASGIApp:
    """Create a bearer-protected, stateless MCP inventory service."""
    server = MCPServer("ebiz-local-dev-inventory", version="1.0.0", log_level="WARNING")

    @server.tool(name="query_sku_identity", structured_output=True)
    async def sku_identity(marketScope: str, sku: str, snapshotTime: str) -> dict[str, Any]:  # noqa: N803
        return _envelope(
            marketScope,
            snapshotTime,
            {"status": "UNIQUE", "canonicalSku": sku, "sellerSkus": [], "mskus": []},
            "sku-identity-v1",
        )

    @server.tool(name="query_inventory_summary", structured_output=True)
    async def inventory_summary(marketScope: str, sku: str, snapshotTime: str) -> dict[str, Any]:  # noqa: N803
        return _envelope(
            marketScope,
            snapshotTime,
            {
                "sku": sku,
                "availableQuantity": 18,
                "holdQuantity": 0,
                "transferInTransitQuantity": 0,
                "purchaseInTransitQuantity": 12,
                "agedInventoryQuantity": 0,
                "daysSinceLastSale": 1,
                "sourceSnapshotId": "local-inventory-snapshot-1",
                "asOf": snapshotTime,
            },
            "inventory-summary-v1",
        )

    @server.tool(name="query_sku_sales_profit_windows", structured_output=True)
    async def sales_windows(marketScope: str, sku: str, snapshotTime: str) -> dict[str, Any]:  # noqa: N803
        return _sales_windows_envelope(marketScope, sku, snapshotTime)

    @server.tool(name="query_sku_sales_profit_windows_batch", structured_output=True)
    async def sales_windows_batch(
        marketScope: str,
        skus: list[str],
        snapshotTime: str,  # noqa: N803
    ) -> dict[str, Any]:
        if not 1 <= len(skus) <= 1000:
            raise ValueError("batch size must be 1..1000")
        return {
            "contractVersion": "erp-supply-chain/v1",
            "marketScope": marketScope,
            "snapshotTime": snapshotTime,
            "items": [_sales_windows_envelope(marketScope, sku, snapshotTime) for sku in skus],
        }

    @server.tool(name="query_sku_boston_cohort", structured_output=True)
    async def boston_cohort(
        marketScope: str,
        snapshotTime: str,
        targetSku: str,  # noqa: N803
    ) -> dict[str, Any]:
        members = [
            {
                "sku": f"SKU-LOCAL-PEER-{index}",
                "windows": _window_rows(),
                "activeDays": 120,
                "growthRatio": 0.05 + index / 100,
            }
            for index in range(1, 6)
        ]
        return _envelope(
            marketScope,
            snapshotTime,
            {
                "targetSku": targetSku,
                "status": "AVAILABLE",
                "filterDefinition": "NA_COMPANY_ACTIVE_60D_V1",
                "totalEligible": len(members),
                "members": members,
                "nextCursor": None,
                "cohortSnapshotId": "local-cohort-snapshot-1",
            },
            "sku-sales-profit-v1",
        )

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="127.0.0.1",
    )
    return _BearerProtectedApp(cast(ASGIApp, app))


def _required_text(body: dict[str, Any], name: str) -> str:
    value = body[name]
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be non-empty text")
    return value


def _envelope(
    market_scope: str, snapshot_time: str, result: dict[str, Any], statistics_version: str
) -> dict[str, Any]:
    return {
        "contractVersion": "erp-supply-chain/v1",
        "statisticsVersion": statistics_version,
        "statisticsDefinition": {
            "variance": "POPULATION",
            "missingDays": "ZERO_WITH_COMPLETE_WATERMARK",
            "windowBoundary": "CLOSED_INCLUSIVE",
            "timezone": "UTC",
        },
        "marketScope": market_scope,
        "snapshotTime": snapshot_time,
        "observedAt": snapshot_time,
        "result": result,
    }


def _window_rows() -> list[dict[str, Any]]:
    return [
        {
            "windowDays": days,
            "unitsSum": float(days * 2),
            "meanDailyUnits": 2.0,
            "populationVariance": 0.25,
            "revenue": float(days * 20),
            "purchaseCost": float(days * 12),
            "grossProfit": float(days * 8),
            "grossMarginRatio": 0.4,
            "grossMarginUnavailableReason": "NONE",
        }
        for days in (7, 14, 30, 60, 90, 180, 365)
    ]


def _sales_windows_envelope(market_scope: str, sku: str, snapshot_time: str) -> dict[str, Any]:
    return _envelope(
        market_scope,
        snapshot_time,
        {
            "sku": sku,
            "status": "FOUND",
            "windows": _window_rows(),
            "activeDays": 120,
            "growthRatio": 0.1,
            "growthUnavailableReason": "NONE",
            "currency": "USD",
            "dataAsOf": snapshot_time,
            "sourceWatermark": "local-sales-watermark-1",
        },
        "sku-sales-profit-v1",
    )


async def _serve(
    provider_port: int,
    mcp_port: int,
    broker_port: int,
    *,
    tls_cert: str,
    tls_key: str,
) -> None:
    provider = uvicorn.Server(
        uvicorn.Config(
            create_provider_app(),
            host="127.0.0.1",
            port=provider_port,
            log_level="info",
            ssl_certfile=tls_cert,
            ssl_keyfile=tls_key,
        )
    )
    mcp = uvicorn.Server(
        uvicorn.Config(create_mcp_app(), host="127.0.0.1", port=mcp_port, log_level="info")
    )
    broker = uvicorn.Server(
        uvicorn.Config(create_provider_app(), host="127.0.0.1", port=broker_port, log_level="info")
    )
    await asyncio.gather(provider.serve(), mcp.serve(), broker.serve())


def main() -> int:
    """Run both loopback services until interrupted."""
    tls_cert = os.environ.get("LOCAL_DEV_TLS_CERT_FILE", "")
    tls_key = os.environ.get("LOCAL_DEV_TLS_KEY_FILE", "")
    if not tls_cert or not tls_key:
        print("LOCAL_DEV_E2E TLS assets are required", file=sys.stderr)
        return 2
    try:
        asyncio.run(_serve(18080, 18081, 18082, tls_cert=tls_cert, tls_key=tls_key))
    except KeyboardInterrupt:
        return 130
    return 0


__all__ = [
    "BROKER_AUTH_TOKEN",
    "CREDENTIAL_REF",
    "OPENAI_API_KEY",
    "REQUEST_ACCESS_TOKEN",
    "create_mcp_app",
    "create_provider_app",
    "main",
]
