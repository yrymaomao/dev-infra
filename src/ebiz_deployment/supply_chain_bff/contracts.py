"""Versioned public BFF request and response contracts."""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SKU = re.compile(r"^[^,\s]{1,128}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BatchCreateRequest(StrictModel):
    skus: tuple[str, ...] = Field(min_length=1, max_length=200)
    marketplace: Literal["US"] = "US"
    fulfillment_mode: Literal["FBM"] = "FBM"
    client_request_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=200,
    )

    @field_validator("skus", mode="before")
    @classmethod
    def normalize_skus(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or len(value) > 200:
            raise ValueError("skus must contain between 1 and 200 values")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("each SKU must be a string")
            sku = raw.strip()
            if _SKU.fullmatch(sku) is None:
                raise ValueError("each SKU must be non-empty and contain no whitespace")
            if sku not in seen:
                seen.add(sku)
                normalized.append(sku)
        if not normalized:
            raise ValueError("at least one SKU is required")
        return tuple(normalized)


class EtaPayload(StrictModel):
    low_seconds: int = Field(ge=0)
    high_seconds: int = Field(ge=0)
    profile_version: str
    dynamic: bool


class RuntimeProfilePayload(StrictModel):
    schema_version: Literal["business-agent.runtime-profile.v1"] = (
        "business-agent.runtime-profile.v1"
    )
    max_batch_size: int = 200
    tenant_dispatch_concurrency: int = 4
    marketplace: Literal["US"] = "US"
    fulfillment_mode: Literal["FBM"] = "FBM"
    eta_profile_version: str
    fixed_seconds: float
    per_item_seconds: float
    uncertainty_ratio: float
    async_start_enabled: bool
    stream_enabled: bool
    activity_ui_enabled: bool
    model_error_polish_enabled: bool


class FeedbackFacts(StrictModel):
    error_code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_.-]*$")
    category: Literal[
        "validation",
        "authorization",
        "configuration",
        "transient",
        "permanent",
        "conflict",
    ]
    phase: Literal["validation", "routing", "invocation", "persistence", "governance", "recovery"]
    retryable: bool
    safe_message: str = Field(min_length=1, max_length=1024)


class FeedbackRequest(StrictModel):
    request_id: UUID = Field(strict=False)
    locale: Literal["en-US", "zh-CN"]
    operation: Literal["START_ANALYSIS_BATCH"]
    facts: FeedbackFacts


class FeedbackResponse(StrictModel):
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=2000)
    cause: str = Field(min_length=1, max_length=2000)
    next_steps: list[str] = Field(min_length=1, max_length=5)
    retryable: bool
    request_id: UUID = Field(strict=False)
