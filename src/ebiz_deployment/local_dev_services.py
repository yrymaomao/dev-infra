"""Loopback-only deterministic services for ``LOCAL_DEV_E2E``.

These services deliberately exercise the production HTTP and MCP adapters while
remaining isolated from ERP and paid model endpoints.  The credentials below
are fixed development sentinels, not user or production credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal, cast

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp.server import MCPServer
from pydantic import AfterValidator, Field
from starlette.types import ASGIApp, Receive, Scope, Send

BROKER_AUTH_TOKEN = "local-dev-broker-auth-not-production"
CREDENTIAL_REF = "local-dev-credential-ref"
REQUEST_ACCESS_TOKEN = "local-dev-request-access-not-production"
OPENAI_API_KEY = "local-dev-openai-key-not-production"
LOCAL_TENANT_ID = "tenant-local-dev"
LOCAL_NO_WINDOW_DATA_SKU = "SKU-LOCAL-NO-WINDOW-DATA"


def _not_blank(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _valid_yyyymmdd(value: str) -> str:
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        raise ValueError("must be a valid yyyyMMdd calendar date") from None
    return value


MarketScope = Literal["NA_COMPANY"]
Sku = Annotated[str, Field(min_length=1, max_length=128), AfterValidator(_not_blank)]
PageSize = Annotated[int, Field(ge=1, le=1000)]
PageIndex = Annotated[int, Field(ge=1)]
SnapshotDate = Annotated[str, Field(pattern=r"^\d{8}$"), AfterValidator(_valid_yyyymmdd)]
ExactSearch = Literal["exactSearch"]
SkuBatch = Annotated[list[Sku], Field(min_length=1, max_length=200)]
Level2PageSize = Annotated[int, Field(ge=1, le=200)]
Threshold = Annotated[int, Field(ge=0)]
SnapshotRef = Annotated[str, Field(min_length=1, max_length=256)]
Cursor = Annotated[str, Field(min_length=1, max_length=2048)]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
_CANONICAL_RFC3339_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_ALLOWED_CREDENTIAL_PROVIDERS = frozenset({"mcp.streamable_http", "yeaher.erp"})


def _bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="local development authentication required")
    return authorization.removeprefix("Bearer ")


def _local_inventory_as_of() -> str:
    """Use the generated replay clock, never wall-clock time, for local inventory freshness."""

    value = os.environ.get("SUPPLY_CHAIN_SNAPSHOT_TIME")
    if value is None:
        raise ValueError("SUPPLY_CHAIN_SNAPSHOT_TIME is required")
    if _CANONICAL_RFC3339_UTC_SECONDS.fullmatch(value) is None:
        raise ValueError("SUPPLY_CHAIN_SNAPSHOT_TIME must be canonical RFC3339 UTC seconds")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        message = "SUPPLY_CHAIN_SNAPSHOT_TIME must be canonical RFC3339 UTC seconds"
        raise ValueError(message) from None
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ValueError("SUPPLY_CHAIN_SNAPSHOT_TIME must be canonical RFC3339 UTC seconds")
    return canonical


def _last_completed_utc_day(snapshot_date: str) -> str:
    """Return the deterministic source date before a run's snapshot day.

    The Base ERP adapter maps a ``yyyyMMdd`` source maximum to the final
    second of that UTC day. A local run cannot claim its still-open snapshot
    day as completed source data without creating future evidence.
    """

    snapshot = datetime.strptime(snapshot_date, "%Y%m%d").replace(tzinfo=UTC)
    return (snapshot - timedelta(days=1)).strftime("%Y%m%d")


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
        output = _deterministic_model_output(body)
        _validate_structured_model_request(body, output)
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


def _deterministic_model_output(body: dict[str, Any]) -> dict[str, Any]:
    """Select one schema-authorized fixture value without inventing business facts."""

    try:
        schema = body["text"]["format"]["schema"]
        properties = schema["properties"]
        issue_code = properties.get("issue_code")
        message = properties.get("message")
        if isinstance(issue_code, dict) and isinstance(message, dict):
            code = issue_code["const"]
            allowed_messages = message["enum"]
            if not isinstance(code, str) or not isinstance(allowed_messages, list):
                raise ValueError
            selected = allowed_messages[0]
            if not isinstance(selected, str):
                raise ValueError
            return {
                "issue_code": code,
                "message": selected,
                "blocking": True,
                "metadata": {},
            }
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    return {
        "explanation": "Deterministic local development analysis.",
        "risk_flags": [],
    }


def _validate_structured_model_request(body: dict[str, Any], output: dict[str, Any]) -> None:
    try:
        text = body["text"]
        if not isinstance(text, dict) or set(text) != {"format"}:
            raise ValueError
        response_format = text["format"]
        if not isinstance(response_format, dict) or set(response_format) != {
            "type",
            "name",
            "schema",
            "strict",
        }:
            raise ValueError
        if (
            response_format["type"] != "json_schema"
            or response_format["name"] != "structured_output"
            or response_format["strict"] is not True
        ):
            raise ValueError
        schema = response_format["schema"]
        if not isinstance(schema, dict):
            raise ValueError
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(output)
    except (KeyError, SchemaError, ValidationError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="invalid local structured model request",
        ) from None


class _McpKeyProtectedApp:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            if headers.get(b"x-mcp-key") != REQUEST_ACCESS_TOKEN.encode():
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


def _closed_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _window_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "windowDays": {"type": "integer", "enum": [7, 14, 30, 60, 90, 180, 365]},
            "unitsSum": {"type": "number", "minimum": 0},
            "meanDailyUnits": {"type": "number", "minimum": 0},
            "populationVariance": {"type": "number", "minimum": 0},
            "revenue": {"type": "number", "minimum": 0},
            "purchaseCost": {"type": "number", "minimum": 0},
            "grossProfit": {"type": "number"},
            "grossMarginRatio": {"type": ["number", "null"]},
            "grossMarginUnavailableReason": {
                "type": "string",
                "enum": ["NONE", "ZERO_REVENUE"],
            },
        }
    )


def _sales_item_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "skuCode": {"type": "string", "minLength": 1, "maxLength": 128},
            "status": {
                "type": "string",
                "enum": ["FOUND", "SKU_NOT_FOUND", "NO_WINDOW_DATA", "INCOMPLETE"],
            },
            "incompleteReason": {"type": ["string", "null"]},
            "windows": {
                "type": "array",
                "maxItems": 7,
                "items": _window_schema(),
            },
            "activeDays": {"type": ["integer", "null"], "minimum": 0},
            "growthRatio": {"type": ["number", "null"]},
            "growthUnavailableReason": {
                "type": ["string", "null"],
                "enum": ["NONE", "ZERO_BASE", "INSUFFICIENT_HISTORY", None],
            },
            "currency": {"type": "string", "const": "USD"},
            "sourceMaxBizDate": {"type": ["string", "null"], "pattern": "^\\d{8}$"},
            "sourceWatermark": {"type": ["string", "null"], "minLength": 1},
            "calculationVersion": {"type": "string", "const": "sku-sales-profit-v1"},
        }
    )


def _yeaher_result_schema(result: dict[str, Any]) -> dict[str, Any]:
    return _closed_schema(
        {
            "success": {"type": "boolean", "const": True},
            "code": {"type": "integer", "const": 200},
            "message": {"type": "null"},
            "result": result,
        }
    )


def _result_data_schema(result: dict[str, Any]) -> dict[str, Any]:
    return _closed_schema(
        {
            "code": {"type": "integer", "const": 200},
            "message": {"type": "string", "const": "success"},
            "result": result,
        }
    )


def _sku_list_record_schema() -> dict[str, Any]:
    nullable_text = {"type": ["string", "null"]}
    nullable_integer = {"type": ["integer", "null"]}
    category = _closed_schema(
        {
            "amazonCategoryId": nullable_text,
            "amazonCategoryName": nullable_text,
            "categoryId": nullable_text,
            "fullPath": nullable_text,
        }
    )
    sub_sku = _closed_schema(
        {
            "skuCode": nullable_text,
            "skuName": nullable_text,
            "quantity": nullable_text,
            "barCode": nullable_text,
            "newStatus": nullable_text,
            "productType": nullable_text,
            "subSkuResultDtos": {"type": ["array", "null"], "items": {"type": "object"}},
            "isContainUpgrade": nullable_text,
            "isToBeDeprecated": nullable_integer,
            "isUpgradable": nullable_integer,
            "isAccessory": nullable_integer,
        }
    )
    image = _closed_schema(
        {
            "skuCode": nullable_text,
            "imageSeq": {"type": "integer"},
            "imageUrlHd": nullable_text,
            "imageUrlThumb": nullable_text,
            "imageUrl": nullable_text,
            "imageType": nullable_text,
        }
    )
    return _closed_schema(
        {
            "skuName": nullable_text,
            "barCode": {"type": "string", "minLength": 1, "maxLength": 128},
            "skuCode": {"type": "string", "minLength": 1, "maxLength": 128},
            "manager": nullable_text,
            "newStatus": nullable_text,
            "currency": nullable_text,
            "cost": {"type": ["number", "null"]},
            "spuCode": nullable_text,
            "spuName": nullable_text,
            "brand": nullable_text,
            "category": nullable_text,
            "note": nullable_text,
            "productType": nullable_text,
            "isContainUpgrade": nullable_text,
            "isAccessory": nullable_integer,
            "isUpgradeAccessory": nullable_integer,
            "categoryId": nullable_text,
            "amazonCategoryName": nullable_text,
            "path": {"type": ["array", "null"], "items": category},
            "fullPath": nullable_text,
            "isToBeDeprecated": nullable_integer,
            "isUpgradable": nullable_integer,
            "subSkuResultDtos": {"type": ["array", "null"], "items": sub_sku},
            "imageUrls": {"type": ["array", "null"], "items": image},
            "upid": nullable_text,
            "createTime": nullable_text,
            "createBy": nullable_text,
        }
    )


def _formal_output_schemas() -> dict[str, dict[str, Any]]:
    identity_page = _closed_schema(
        {
            "records": {
                "type": "array",
                "maxItems": 1000,
                "items": _sku_list_record_schema(),
            },
            "total": {"type": "integer", "minimum": 0},
            "current": {"type": "integer", "minimum": 1},
            "size": {"type": "integer", "minimum": 1},
            "pages": {"type": "integer", "minimum": 0},
        }
    )
    inventory_properties: dict[str, Any] = {
        "status": {"type": "string", "enum": ["FOUND", "NO_SNAPSHOT"]},
        "sku": {"type": "string", "minLength": 1},
        "availableQuantity": {"type": "number", "minimum": 0},
        "holdQuantity": {"type": "number", "minimum": 0},
        "transferInTransitQuantity": {"type": "number", "minimum": 0},
        "purchaseInTransitQuantity": {"type": "number", "minimum": 0},
        "agedInventoryQuantity": {"type": "number", "minimum": 0},
        "daysSinceLastSale": {"type": ["integer", "null"], "minimum": 0},
        "sourceSnapshotId": {"type": "string", "minLength": 1},
        "asOf": {"type": "string", "format": "date-time"},
    }
    cohort_member = _closed_schema(
        {
            "skuCode": {"type": "string", "minLength": 1, "maxLength": 128},
            "windows": {
                "type": "array",
                "minItems": 7,
                "maxItems": 7,
                "items": _window_schema(),
            },
            "activeDays": {"type": "integer", "minimum": 60},
            "growthRatio": {"type": "number"},
        }
    )
    cohort = _closed_schema(
        {
            "status": {
                "type": "string",
                "enum": ["AVAILABLE", "INSUFFICIENT", "TOO_LARGE", "INCOMPLETE"],
            },
            "filterDefinition": {
                "type": "string",
                "const": "NA_COMPANY_ACTIVE_60D_V1",
            },
            "totalEligible": {"type": "integer", "minimum": 0},
            "snapshotId": {"type": "string", "minLength": 1},
            "sourceMaxBizDate": {"type": "string", "pattern": "^\\d{8}$"},
            "sourceWatermark": {"type": "string", "minLength": 1},
            "incompleteReason": {"type": ["string", "null"]},
            "nextCursor": {"type": ["string", "null"], "minLength": 1},
            "members": {"type": "array", "maxItems": 1000, "items": cohort_member},
        }
    )
    batch = _closed_schema(
        {
            "cid": {"type": "string", "const": LOCAL_TENANT_ID},
            "marketScope": {"type": "string", "const": "NA_COMPANY"},
            "snapshotDate": {"type": "string", "pattern": "^\\d{8}$"},
            "statisticsVersion": {"type": "string", "const": "sku-sales-profit-v1"},
            "currency": {"type": "string", "const": "USD"},
            "items": {"type": "array", "minItems": 1, "maxItems": 1, "items": _sales_item_schema()},
        }
    )
    cohort_page = _closed_schema(
        {
            "cid": {"type": "string", "const": LOCAL_TENANT_ID},
            "marketScope": {"type": "string", "const": "NA_COMPANY"},
            "snapshotDate": {"type": "string", "pattern": "^\\d{8}$"},
            "statisticsVersion": {"type": "string", "const": "sku-sales-profit-v1"},
            "currency": {"type": "string", "const": "USD"},
            "filterDefinition": cohort["properties"]["filterDefinition"],
            "status": cohort["properties"]["status"],
            "totalEligible": cohort["properties"]["totalEligible"],
            "snapshotId": cohort["properties"]["snapshotId"],
            "sourceMaxBizDate": cohort["properties"]["sourceMaxBizDate"],
            "sourceWatermark": cohort["properties"]["sourceWatermark"],
            "incompleteReason": cohort["properties"]["incompleteReason"],
            "members": cohort["properties"]["members"],
            "nextCursor": cohort["properties"]["nextCursor"],
        }
    )
    sku = {"type": "string", "minLength": 1, "maxLength": 128}
    nullable_nonnegative_integer = {"type": ["integer", "null"], "minimum": 0}
    threshold_page = _closed_schema(
        {
            "schema_version": {"const": "supply-chain.inventory-threshold.v1"},
            "source_snapshot_id": {"type": "string", "minLength": 1},
            "snapshot_time": {"type": "string", "format": "date-time"},
            "items": {
                "type": "array",
                "maxItems": 200,
                "items": _closed_schema(
                    {"sku": sku, "available_quantity": {"type": "integer", "minimum": 0}}
                ),
            },
            "next_cursor": {"type": ["string", "null"]},
        }
    )
    inventory_batch_item = _closed_schema(
        {
            "status": {"enum": ["FOUND", "NO_SNAPSHOT"]},
            "sku": sku,
            "availableQuantity": nullable_nonnegative_integer,
            "holdQuantity": nullable_nonnegative_integer,
            "transferInTransitQuantity": nullable_nonnegative_integer,
            "purchaseInTransitQuantity": nullable_nonnegative_integer,
            "agedInventoryQuantity": nullable_nonnegative_integer,
            "daysSinceLastSale": nullable_nonnegative_integer,
            "sourceSnapshotId": {"type": ["string", "null"]},
            "asOf": {"type": ["string", "null"], "format": "date-time"},
        }
    )
    inventory_batch = _closed_schema(
        {
            "schemaVersion": {"const": "supply-chain.inventory-batch-snapshot.v1"},
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 200,
                "items": inventory_batch_item,
            },
        }
    )
    identity_item = _closed_schema(
        {
            "sku": sku,
            "status": {"enum": ["FOUND", "NO_FOUND", "CONFLICT"]},
            "msku_status": {"enum": ["FOUND", "NO_MAPPING", "CONFLICT", "NOT_APPLICABLE"]},
            "msku": {"type": ["string", "null"]},
            "asin_status": {"const": "UNAVAILABLE_NOT_OWNED"},
            "asins": {"type": ["array", "null"], "items": {"type": "string"}},
            "source": {"type": "string", "minLength": 1},
            "valid_from": {"type": ["string", "null"], "format": "date-time"},
            "valid_to": {"type": ["string", "null"], "format": "date-time"},
        }
    )
    identity_batch = _closed_schema(
        {
            "schema_version": {"const": "supply-chain.sku-identity-mapping.v1"},
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 200,
                "items": identity_item,
            },
        }
    )
    age_buckets = _closed_schema(
        {
            "days_0_90": nullable_nonnegative_integer,
            "days_91_180": nullable_nonnegative_integer,
            "days_181_270": nullable_nonnegative_integer,
            "days_271_365": nullable_nonnegative_integer,
            "days_365_plus": nullable_nonnegative_integer,
        }
    )
    fba_item = _closed_schema(
        {
            "sku": sku,
            "status": {
                "enum": [
                    "FOUND",
                    "NO_SNAPSHOT",
                    "SNAPSHOT_INCOMPLETE",
                    "SNAPSHOT_CONFLICT",
                ]
            },
            "msku": {"type": ["string", "null"]},
            "fnsku": {"type": ["string", "null"]},
            "asin": {"type": ["string", "null"]},
            "available": nullable_nonnegative_integer,
            "reserved": nullable_nonnegative_integer,
            "inbound": nullable_nonnegative_integer,
            "unfulfillable": nullable_nonnegative_integer,
            "age_buckets": age_buckets,
            "monthly_storage_cost": {"type": ["number", "null"], "minimum": 0},
        }
    )
    fba_batch = _closed_schema(
        {
            "schema_version": {"const": "supply-chain.fba-inventory-snapshot.v1"},
            "source_snapshot_id": {"type": "string", "minLength": 1},
            "snapshot_time": {"type": "string", "format": "date-time"},
            "items": {"type": "array", "minItems": 1, "maxItems": 200, "items": fba_item},
        }
    )
    fulfillment_sale = _closed_schema(
        {
            "sku": sku,
            "business_week": {"type": "string", "format": "date"},
            "fulfillment": {"enum": ["FBA", "FBM", "UNKNOWN"]},
            "units": {"type": "number"},
            "revenue": {"type": "number"},
            "profit": {"type": ["number", "null"]},
            "returns": {"type": "number", "minimum": 0},
            "discount": {"type": "number", "minimum": 0},
        }
    )
    fulfillment_sales = _closed_schema(
        {
            "schema_version": {"const": "supply-chain.fulfillment-sales-profit-windows.v2"},
            "source_snapshot_id": {"type": "string", "minLength": 1},
            "latest_complete_week": {"type": "string", "format": "date"},
            "items": {"type": "array", "items": fulfillment_sale},
        }
    )
    return {
        "query_sku_upc_mapping": _yeaher_result_schema(identity_page),
        "query_inventory_summary_v2": _result_data_schema(_closed_schema(inventory_properties)),
        "query_sku_sales_profit_windows_v1": _yeaher_result_schema(batch),
        "query_sku_boston_cohort_v1": _yeaher_result_schema(cohort_page),
        "query_inventory_skus_by_threshold_v1": _yeaher_result_schema(threshold_page),
        "query_inventory_batch_snapshot_v1": _yeaher_result_schema(inventory_batch),
        "query_sku_identity_mapping_v1": _yeaher_result_schema(identity_batch),
        "query_sku_fulfillment_inventory_snapshot_v1": _yeaher_result_schema(fba_batch),
        "query_sku_fulfillment_sales_profit_windows_v2": _yeaher_result_schema(fulfillment_sales),
    }


def _apply_formal_tool_schemas(server: MCPServer) -> None:
    for name, output_schema in _formal_output_schemas().items():
        tool = server._tool_manager.get_tool(name)  # noqa: SLF001 - fixture contract registration
        if tool is None:
            raise RuntimeError(f"local MCP tool {name!r} is not registered")
        argument_model = tool.fn_metadata.arg_model
        argument_model.model_config["extra"] = "forbid"
        argument_model.model_rebuild(force=True)
        tool.parameters = argument_model.model_json_schema(by_alias=True)
        tool.fn_metadata.output_schema = output_schema
        tool.__dict__.pop("output_schema", None)


def create_mcp_app() -> ASGIApp:
    """Create an X-Mcp-Key protected, stateless MCP inventory service."""
    server = MCPServer("ebiz-local-dev-inventory", version="1.0.0", log_level="WARNING")
    inventory_as_of = _local_inventory_as_of()

    @server.tool(name="query_sku_upc_mapping", structured_output=True)
    async def sku_upc_mapping(
        pageIndex: PageIndex,  # noqa: N803 - Java MCP wire field
        pageSize: PageSize,  # noqa: N803 - Java MCP wire field
        skuCode: Annotated[list[Sku], Field(min_length=1, max_length=1000)],  # noqa: N803
        searchType: ExactSearch,  # noqa: N803 - Java MCP wire field
    ) -> dict[str, Any]:
        if len(set(skuCode)) != len(skuCode):  # noqa: N803 - Java MCP wire field
            raise ValueError("skuCode must be unique")
        del searchType
        return _yeaher_result(
            {
                "records": [_sku_list_record(sku) for sku in skuCode],
                "total": len(skuCode),
                "current": pageIndex,
                "size": pageSize,
                "pages": 1,
            }
        )

    @server.tool(name="query_inventory_summary_v2", structured_output=True)
    async def inventory_summary_v2(
        marketScope: MarketScope,
        skuCode: Sku,  # noqa: N803 - Java MCP wire field
    ) -> dict[str, Any]:
        del marketScope
        return _result_data(
            {
                "status": "FOUND",
                "sku": skuCode,
                "availableQuantity": 18,
                "holdQuantity": 0,
                "transferInTransitQuantity": 0,
                "purchaseInTransitQuantity": 12,
                "agedInventoryQuantity": 0,
                "daysSinceLastSale": 1,
                "sourceSnapshotId": "local-inventory-snapshot-1",
                "asOf": inventory_as_of,
            }
        )

    @server.tool(name="query_sku_sales_profit_windows_v1", structured_output=True)
    async def sales_windows(
        marketScope: MarketScope,
        skuCode: Sku,  # noqa: N803 - Java MCP wire field
        snapshotDate: SnapshotDate,  # noqa: N803 - Java MCP wire field
    ) -> dict[str, Any]:
        del marketScope
        return _yeaher_result(_sales_windows_batch(skuCode, snapshotDate))

    @server.tool(name="query_sku_boston_cohort_v1", structured_output=True)
    async def boston_cohort(
        marketScope: MarketScope,
        snapshotDate: SnapshotDate,  # noqa: N803 - Java MCP wire field
        targetSkuCode: Sku,  # noqa: N803 - Java MCP wire field
        pageSize: PageSize,  # noqa: N803 - Java MCP wire field
        cursor: Annotated[str | None, Field(min_length=1, max_length=4096)] = None,
    ) -> dict[str, Any]:
        del marketScope, pageSize, targetSkuCode
        if cursor is not None:
            raise ValueError("deterministic local cohort has exactly one page")
        source_max_biz_date = _last_completed_utc_day(snapshotDate)
        members = [
            {
                "skuCode": f"SKU-LOCAL-PEER-{index}",
                "windows": _window_rows(),
                "activeDays": 120,
                "growthRatio": 0.05 + index / 100,
            }
            for index in range(1, 6)
        ]
        return _yeaher_result(
            {
                "cid": LOCAL_TENANT_ID,
                "marketScope": "NA_COMPANY",
                "snapshotDate": snapshotDate,
                "statisticsVersion": "sku-sales-profit-v1",
                "currency": "USD",
                "status": "AVAILABLE",
                "filterDefinition": "NA_COMPANY_ACTIVE_60D_V1",
                "totalEligible": len(members),
                "members": members,
                "nextCursor": None,
                "snapshotId": "local-cohort-snapshot-1",
                "sourceMaxBizDate": source_max_biz_date,
                "sourceWatermark": f"local-cohort-watermark-{source_max_biz_date}",
                "incompleteReason": None,
            }
        )

    @server.tool(name="query_inventory_skus_by_threshold_v1", structured_output=True)
    async def inventory_skus_by_threshold(
        quantity_metric: Literal["AVAILABLE_QUANTITY"],
        operator: Literal["GT"],
        threshold: Threshold,
        sort: Literal["SKU_ASC"],
        page_size: Level2PageSize,
        source_snapshot_id: SnapshotRef | None = None,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        del quantity_metric, operator, sort
        snapshot_id = "local-ims-level2-snapshot-1"
        if source_snapshot_id not in {None, snapshot_id}:
            raise ValueError("source_snapshot_id does not match the frozen local snapshot")
        if cursor is not None:
            raise ValueError("deterministic local selection has exactly one page")
        candidates = (
            ("SKU-LOCAL-FBA", 31),
            ("SKU-LOCAL-FBM", 30),
            ("SKU-LOCAL-MIXED", 60),
        )
        items = [
            {"sku": sku, "available_quantity": quantity}
            for sku, quantity in candidates
            if quantity > threshold
        ][:page_size]
        return _yeaher_result(
            {
                "schema_version": "supply-chain.inventory-threshold.v1",
                "source_snapshot_id": snapshot_id,
                "snapshot_time": inventory_as_of,
                "items": items,
                "next_cursor": None,
            }
        )

    @server.tool(name="query_inventory_batch_snapshot_v1", structured_output=True)
    async def inventory_batch_snapshot(skus: SkuBatch) -> dict[str, Any]:
        if len(skus) != len(set(skus)):
            raise ValueError("skus must be unique")
        return _yeaher_result(
            {
                "schemaVersion": "supply-chain.inventory-batch-snapshot.v1",
                "items": [
                    {
                        "status": "FOUND",
                        "sku": sku,
                        "availableQuantity": 30 if "MIXED" not in sku else 48,
                        "holdQuantity": 0,
                        "transferInTransitQuantity": 0,
                        "purchaseInTransitQuantity": 12,
                        "agedInventoryQuantity": 4,
                        "daysSinceLastSale": 1,
                        "sourceSnapshotId": "local-ims-level2-snapshot-1",
                        "asOf": inventory_as_of,
                    }
                    for sku in skus
                ],
            }
        )

    @server.tool(name="query_sku_identity_mapping_v1", structured_output=True)
    async def sku_identity_mapping(skus: SkuBatch) -> dict[str, Any]:
        if len(skus) != len(set(skus)):
            raise ValueError("skus must be unique")
        return _yeaher_result(
            {
                "schema_version": "supply-chain.sku-identity-mapping.v1",
                "items": [
                    {
                        "sku": sku,
                        "status": "FOUND",
                        "msku_status": "FOUND",
                        "msku": f"MSKU-{sku.removeprefix('SKU-')}",
                        "asin_status": "UNAVAILABLE_NOT_OWNED",
                        "asins": None,
                        "source": "LOCAL_DETERMINISTIC:SKU_MAPPING",
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": None,
                    }
                    for sku in skus
                ],
            }
        )

    @server.tool(name="query_sku_fulfillment_inventory_snapshot_v1", structured_output=True)
    async def fba_inventory_snapshot(skus: SkuBatch) -> dict[str, Any]:
        if len(skus) != len(set(skus)):
            raise ValueError("skus must be unique")
        missing_ages = {
            "days_0_90": None,
            "days_91_180": None,
            "days_181_270": None,
            "days_271_365": None,
            "days_365_plus": None,
        }
        items: list[dict[str, Any]] = []
        for sku in skus:
            has_fba = "FBA" in sku or "MIXED" in sku
            items.append(
                {
                    "sku": sku,
                    "status": "FOUND" if has_fba else "NO_SNAPSHOT",
                    "msku": f"MSKU-{sku.removeprefix('SKU-')}" if has_fba else None,
                    "fnsku": f"FNSKU-{sku.removeprefix('SKU-')}" if has_fba else None,
                    "asin": "B000000001" if has_fba else None,
                    "available": 21 if "FBA" in sku else (12 if has_fba else None),
                    "reserved": 2 if has_fba else None,
                    "inbound": 5 if has_fba else None,
                    "unfulfillable": 1 if has_fba else None,
                    "age_buckets": dict(missing_ages),
                    "monthly_storage_cost": 3.5 if has_fba else None,
                }
            )
        return _yeaher_result(
            {
                "schema_version": "supply-chain.fba-inventory-snapshot.v1",
                "source_snapshot_id": "local-fba-level2-snapshot-1",
                "snapshot_time": inventory_as_of,
                "items": items,
            }
        )

    @server.tool(name="query_sku_fulfillment_sales_profit_windows_v2", structured_output=True)
    async def sku_fulfillment_sales_profit_windows(
        skus: SkuBatch,
        week_from: IsoDate,
        week_to: IsoDate,
    ) -> dict[str, Any]:
        if len(skus) != len(set(skus)):
            raise ValueError("skus must be unique")
        try:
            first_week = date.fromisoformat(week_from)
            last_week = date.fromisoformat(week_to)
        except ValueError:
            raise ValueError("week range must use valid ISO dates") from None
        if first_week > last_week:
            raise ValueError("week_from must not follow week_to")
        weeks: list[date] = []
        current = first_week
        while current <= last_week and len(weeks) < 53:
            weeks.append(current)
            current += timedelta(days=7)
        items: list[dict[str, Any]] = []
        for sku in skus:
            fulfillments = (
                ("FBA", "FBM") if "MIXED" in sku else (("FBA",) if "FBA" in sku else ("FBM",))
            )
            for index, business_week in enumerate(weeks):
                for fulfillment in fulfillments:
                    units = 8 + index + (2 if fulfillment == "FBA" else 0)
                    items.append(
                        {
                            "sku": sku,
                            "business_week": business_week.isoformat(),
                            "fulfillment": fulfillment,
                            "units": units,
                            "revenue": float(units * 25),
                            "profit": float(units * 8),
                            "returns": 0.0,
                            "discount": 0.0,
                        }
                    )
        return _yeaher_result(
            {
                "schema_version": "supply-chain.fulfillment-sales-profit-windows.v2",
                "source_snapshot_id": "local-sales-level2-snapshot-1",
                "latest_complete_week": last_week.isoformat(),
                "items": items,
            }
        )

    _apply_formal_tool_schemas(server)

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="127.0.0.1",
    )
    return _McpKeyProtectedApp(cast(ASGIApp, app))


def _required_text(body: dict[str, Any], name: str) -> str:
    value = body[name]
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be non-empty text")
    return value


def _yeaher_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "code": 200,
        "message": None,
        "result": result,
    }


def _result_data(result: dict[str, Any]) -> dict[str, Any]:
    return {"code": 200, "message": "success", "result": result}


def _sku_list_record(sku: str) -> dict[str, Any]:
    bar_code = f"UPC-{sku.removeprefix('SKU-')}"
    return {
        "skuName": "Deterministic local SKU",
        "barCode": bar_code,
        "skuCode": sku,
        "manager": None,
        "newStatus": None,
        "currency": "USD",
        "cost": 12.5,
        "spuCode": None,
        "spuName": None,
        "brand": None,
        "category": None,
        "note": None,
        "productType": None,
        "isContainUpgrade": None,
        "isAccessory": None,
        "isUpgradeAccessory": None,
        "categoryId": None,
        "amazonCategoryName": None,
        "path": [
            {
                "amazonCategoryId": "LOCAL-CATEGORY",
                "amazonCategoryName": "Local category",
                "categoryId": "LOCAL-CATEGORY",
                "fullPath": "Local category",
            }
        ],
        "fullPath": None,
        "isToBeDeprecated": None,
        "isUpgradable": None,
        "subSkuResultDtos": [],
        "imageUrls": [],
        "upid": None,
        "createTime": None,
        "createBy": None,
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


def _sales_windows_batch(sku: str, snapshot_date: str) -> dict[str, Any]:
    if sku == LOCAL_NO_WINDOW_DATA_SKU:
        return {
            "cid": LOCAL_TENANT_ID,
            "marketScope": "NA_COMPANY",
            "snapshotDate": snapshot_date,
            "statisticsVersion": "sku-sales-profit-v1",
            "currency": "USD",
            "items": [
                {
                    "skuCode": sku,
                    "status": "NO_WINDOW_DATA",
                    "incompleteReason": None,
                    "windows": [],
                    "activeDays": None,
                    "growthRatio": None,
                    "growthUnavailableReason": None,
                    "currency": "USD",
                    "sourceMaxBizDate": None,
                    "sourceWatermark": None,
                    "calculationVersion": "sku-sales-profit-v1",
                }
            ],
        }
    source_max_biz_date = _last_completed_utc_day(snapshot_date)
    return {
        "cid": LOCAL_TENANT_ID,
        "marketScope": "NA_COMPANY",
        "snapshotDate": snapshot_date,
        "statisticsVersion": "sku-sales-profit-v1",
        "currency": "USD",
        "items": [
            {
                "skuCode": sku,
                "status": "FOUND",
                "incompleteReason": None,
                "windows": _window_rows(),
                "activeDays": 120,
                "growthRatio": 0.1,
                "growthUnavailableReason": "NONE",
                "currency": "USD",
                "sourceMaxBizDate": source_max_biz_date,
                "sourceWatermark": f"local-sales-watermark-{source_max_biz_date}",
                "calculationVersion": "sku-sales-profit-v1",
            }
        ],
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
