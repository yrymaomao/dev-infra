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
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException, Request
from mcp.server import MCPServer
from starlette.types import ASGIApp, Receive, Scope, Send

BROKER_AUTH_TOKEN = "local-dev-broker-auth-not-production"
CREDENTIAL_REF = "local-dev-credential-ref"
REQUEST_ACCESS_TOKEN = "local-dev-request-access-not-production"
OPENAI_API_KEY = "local-dev-openai-key-not-production"
LOCAL_TENANT_ID = "tenant-local-dev"

_ALLOWED_CREDENTIAL_PROVIDERS = frozenset({"mcp.streamable_http", "yeaher.erp"})
_COCKPIT_PATH = "/ai/read/v1/cockpit/product-performance/sku-windows"


def _bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="local development authentication required")
    return authorization.removeprefix("Bearer ")


def create_provider_app() -> FastAPI:
    """Create credential broker, Cockpit, health, and Responses API routes."""
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
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }

    @app.post(_COCKPIT_PATH)
    async def cockpit_windows(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if request.cookies.get("_token_") != REQUEST_ACCESS_TOKEN:
            raise HTTPException(status_code=401, detail="invalid local ERP session")
        try:
            marketplace = _required_text(body, "marketplace")
            sku = _required_text(body, "sku")
            snapshot_time = _required_text(body, "snapshotTime")
            snapshot = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid Cockpit request") from exc
        if snapshot.tzinfo is None or snapshot.utcoffset() is None or body.get("pageSize") != 200:
            raise HTTPException(status_code=422, detail="invalid Cockpit request")
        return {
            "success": True,
            "result": _sales_profit_result(
                tenant_id=LOCAL_TENANT_ID,
                marketplace=marketplace,
                sku=sku,
                snapshot_time=snapshot.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                snapshot_date=snapshot.astimezone(UTC).date(),
            ),
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

    @server.tool(name="query_inventory_summary", structured_output=True)
    async def inventory_summary(  # noqa: N803
        skuCodes: list[str], pageIndex: int, pageSize: int
    ) -> dict[str, Any]:
        if len(skuCodes) != 1 or pageIndex != 1 or pageSize != 1:
            raise ValueError("invalid deterministic inventory summary request")
        sku = skuCodes[0]
        return {
            "code": 200,
            "success": True,
            "message": "success",
            "result": {
                "pageData": {
                    "records": [{"skuCode": sku, "availableInventory": 18}],
                    "total": 1,
                    "size": 1,
                    "current": 1,
                    "pages": 1,
                },
                "statistics": {"totalAvailableInventory": 18},
            },
        }

    @server.tool(name="query_inventory_by_warehouse", structured_output=True)
    async def inventory_by_warehouse(skuCode: str) -> dict[str, Any]:  # noqa: N803
        return {
            "code": 200,
            "success": True,
            "message": "success",
            "result": [
                {
                    "skuCode": skuCode,
                    "warehouseId": "W-LOCAL-1",
                    "warehouseName": "Local deterministic warehouse 1",
                    "warehouseType": "OWNED",
                    "availableInventory": 10,
                },
                {
                    "skuCode": skuCode,
                    "warehouseId": "W-LOCAL-2",
                    "warehouseName": "Local deterministic warehouse 2",
                    "warehouseType": "THIRD_PARTY",
                    "availableInventory": 8,
                },
            ],
        }

    @server.tool(name="query_purchase_in_transit_details", structured_output=True)
    async def purchase_in_transit(
        skuCode: str, pageIndex: int, pageSize: int  # noqa: N803
    ) -> dict[str, Any]:
        if pageIndex != 1 or pageSize != 200:
            raise ValueError("invalid deterministic purchase-in-transit request")
        return {
            "code": 200,
            "success": True,
            "message": "success",
            "result": {
                "records": [
                    {
                        "poCode": "PO-LOCAL-1",
                        "invoice": "INV-LOCAL-1",
                        "skuCode": skuCode,
                        "shipmentDate": "2026-08-20",
                        "estimatedDeliveryDate": "2026-08-28",
                        "shipmentQuantity": 12,
                        "inboundQuantity": 5,
                        "warehouseId": "W-LOCAL-1",
                        "trackingNumber": "LOCAL-TRACK-1",
                        "status": "IN_TRANSIT",
                    }
                ],
                "total": 1,
                "size": 200,
                "current": 1,
                "pages": 1,
            },
        }

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


def _sales_profit_result(
    *, tenant_id: str, marketplace: str, sku: str, snapshot_time: str, snapshot_date: date
) -> dict[str, Any]:
    first_day = snapshot_date - timedelta(days=729)
    cohort = [f"SKU-LOCAL-PEER-{index}" for index in range(1, 6)]
    return {
        "tenantId": tenant_id,
        "marketplace": marketplace,
        "sku": sku,
        "observedAt": snapshot_time,
        "contractVersion": "cockpit-sku-windows-v1",
        "statisticsVersion": "cockpit-window-statistics-v1",
        "statisticsDefinition": {
            "variance": "POPULATION",
            "missingDays": "ZERO",
            "windowBoundary": "CLOSED_INCLUSIVE",
            "timezone": "UTC",
        },
        "windows": [
            {
                "days": days,
                "weight": weight,
                "mean": float(days),
                "variance": float(days) / 2,
                "unitsSold": days * 2,
            }
            for days, weight in zip(
                (7, 14, 30, 60, 90, 180, 365, 730),
                (0.20, 0.18, 0.16, 0.14, 0.12, 0.10, 0.06, 0.04),
                strict=True,
            )
        ],
        "profit": {
            "currency": "USD",
            "saleAmount": 1000.0,
            "purchaseCost": 600.0,
            "grossProfit": 400.0,
            "grossProfitRatio": 0.4,
        },
        "lifecycleInputs": {"activeDays": 240, "growth": 0.12},
        "seasonality": {
            "status": "AVAILABLE",
            "model": "cockpit-seasonality-v1",
            "points": [
                {
                    "date": (first_day + timedelta(days=index)).isoformat(),
                    "observed": 10.0 + index / 100,
                    "factor": 1.0 + (index % 7) / 100,
                }
                for index in range(730)
            ],
        },
        "bostonCohort": [
            {
                "tenantId": tenant_id,
                "marketplace": marketplace,
                "sku": peer,
                "observedAt": snapshot_time,
                "deseasonalizedMean": 8.0 + index,
                "riskCv": 0.2,
                "growth": 0.05 + index / 100,
                "currency": "USD",
            }
            for index, peer in enumerate(cohort)
        ],
        "nextCursor": None,
        "pageSize": 200,
        "returnedSize": len(cohort),
        "totalSize": len(cohort),
    }


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
