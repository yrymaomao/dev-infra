"""Strict Level 2 BFF contracts generated from the frozen OpenAPI semantics."""

from __future__ import annotations

import re
from datetime import time
from typing import Annotated, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, Field, model_validator

from .contracts import StrictModel

_SKU = re.compile(r"^[^,\s]{1,128}$")


def _canonical_sku(value: str) -> str:
    normalized = value.strip()
    if _SKU.fullmatch(normalized) is None:
        raise ValueError("SKU must be non-empty and contain no comma or whitespace")
    return normalized


CanonicalSku = Annotated[str, AfterValidator(_canonical_sku)]


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise ValueError("timezone is not recognized") from None
    return value


TenantTimezone = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_timezone)]


class InventorySelector(StrictModel):
    quantity_metric: Literal["AVAILABLE_QUANTITY"] = "AVAILABLE_QUANTITY"
    operator: Literal["GT"] = "GT"
    threshold: int = Field(default=20, ge=0)


class SelectionPreviewRequest(StrictModel):
    natural_language: str | None = Field(default=None, min_length=1, max_length=2000)
    selector: InventorySelector | None = None
    client_request_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=200,
    )

    @model_validator(mode="after")
    def exactly_one_input(self) -> SelectionPreviewRequest:
        if (self.natural_language is None) == (self.selector is None):
            raise ValueError("provide exactly one of natural_language or selector")
        return self


class CsvSelectionRow(StrictModel):
    row: int = Field(ge=2)
    sku: CanonicalSku
    fulfillment_mode: Literal["AUTO", "FBA", "FBM", "MIXED"] = "AUTO"
    fba_ratio: float | None = Field(default=None, ge=0, le=1)
    fbm_ratio: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def ratio_pair(self) -> CsvSelectionRow:
        supplied = self.fba_ratio is not None or self.fbm_ratio is not None
        if supplied and (self.fba_ratio is None or self.fbm_ratio is None):
            raise ValueError("fba_ratio and fbm_ratio must be supplied together")
        if supplied:
            assert self.fba_ratio is not None and self.fbm_ratio is not None
            if abs(self.fba_ratio + self.fbm_ratio - 1.0) > 1e-9:
                raise ValueError("fba_ratio and fbm_ratio must sum to 1")
        if supplied and self.fulfillment_mode != "MIXED":
            raise ValueError("ratios are only valid for MIXED fulfillment")
        return self


class CsvRowError(StrictModel):
    row: int = Field(ge=2)
    sku: str | None = None
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1024)


class ReportRunRequest(StrictModel):
    selection_preview_id: UUID = Field(strict=False)
    policy_mode: Literal["ACTIVE_AT_RUN", "PINNED"] = "ACTIVE_AT_RUN"
    policy_version: int | None = Field(default=None, ge=1)
    client_request_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=200,
    )

    @model_validator(mode="after")
    def pinned_policy_requires_version(self) -> ReportRunRequest:
        if (self.policy_mode == "PINNED") != (self.policy_version is not None):
            raise ValueError("PINNED policy mode requires exactly one policy_version")
        return self


class ScheduleCreate(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    timezone: TenantTimezone
    weekday: int = Field(default=1, ge=1, le=7)
    local_time: time = time(hour=12)
    selection_mode: Literal["DYNAMIC_SELECTOR", "FIXED_SKUS"]
    selector: InventorySelector | None = None
    fixed_skus: tuple[CanonicalSku, ...] = Field(default=(), max_length=10_000)
    policy_mode: Literal["ACTIVE_AT_RUN", "PINNED"] = "ACTIVE_AT_RUN"
    policy_version: int | None = Field(default=None, ge=1)
    active: bool = True

    @model_validator(mode="after")
    def consistent_modes(self) -> ScheduleCreate:
        if self.selection_mode == "DYNAMIC_SELECTOR":
            if self.selector is None or self.fixed_skus:
                raise ValueError("dynamic schedules require selector and no fixed_skus")
        elif not self.fixed_skus or self.selector is not None:
            raise ValueError("fixed schedules require fixed_skus and no selector")
        if len(set(self.fixed_skus)) != len(self.fixed_skus):
            raise ValueError("fixed_skus must be unique")
        if (self.policy_mode == "PINNED") != (self.policy_version is not None):
            raise ValueError("PINNED policy mode requires exactly one policy_version")
        return self


class SchedulePatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    timezone: TenantTimezone | None = None
    weekday: int | None = Field(default=None, ge=1, le=7)
    local_time: time | None = None
    active: bool | None = None
    policy_mode: Literal["ACTIVE_AT_RUN", "PINNED"] | None = None
    policy_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def non_empty_patch(self) -> SchedulePatch:
        if not self.model_fields_set:
            raise ValueError("schedule patch must contain at least one field")
        if self.policy_mode == "PINNED" and self.policy_version is None:
            raise ValueError("PINNED policy mode requires policy_version")
        if self.policy_mode == "ACTIVE_AT_RUN" and self.policy_version is not None:
            raise ValueError("ACTIVE_AT_RUN cannot pin policy_version")
        return self
