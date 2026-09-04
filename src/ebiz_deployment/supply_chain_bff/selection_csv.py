"""Bounded CSV parsing with partial row acceptance and conflict-safe deduplication."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from pydantic import ValidationError

from .level2_contracts import CsvRowError, CsvSelectionRow

_HEADERS = ("sku", "fulfillment_mode", "fba_ratio", "fbm_ratio")
MAX_CSV_BYTES = 5 * 1024 * 1024


class CsvFileError(ValueError):
    """The whole CSV is structurally unusable."""


@dataclass(frozen=True, slots=True)
class CsvSelection:
    rows: tuple[CsvSelectionRow, ...]
    errors: tuple[CsvRowError, ...]
    input_row_count: int


def parse_selection_csv(content: bytes, *, max_rows: int = 10_000) -> CsvSelection:
    if not content:
        raise CsvFileError("CSV file is empty")
    if len(content) > MAX_CSV_BYTES:
        raise CsvFileError("CSV exceeds the 5 MiB file limit")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvFileError("CSV must use UTF-8 encoding") from None
    if "\x00" in text:
        raise CsvFileError("CSV contains invalid binary content")
    reader = csv.DictReader(io.StringIO(text), dialect="excel", strict=True)
    if reader.fieldnames is None:
        raise CsvFileError("CSV header is missing")
    normalized_headers = tuple(header.strip() for header in reader.fieldnames)
    if normalized_headers != _HEADERS:
        raise CsvFileError("CSV header must be sku,fulfillment_mode,fba_ratio,fbm_ratio")

    accepted: dict[str, CsvSelectionRow] = {}
    first_rows: dict[str, int] = {}
    conflicted: set[str] = set()
    errors: list[CsvRowError] = []
    input_row_count = 0
    try:
        for row_number, raw in enumerate(reader, start=2):
            input_row_count += 1
            if input_row_count > max_rows:
                raise CsvFileError(f"CSV exceeds the {max_rows} row limit")
            try:
                candidate = {
                    "row": row_number,
                    "sku": raw.get("sku", ""),
                    "fulfillment_mode": (raw.get("fulfillment_mode") or "AUTO").strip().upper(),
                    "fba_ratio": _optional_float(raw.get("fba_ratio")),
                    "fbm_ratio": _optional_float(raw.get("fbm_ratio")),
                }
                parsed = CsvSelectionRow.model_validate(candidate)
            except (ValidationError, ValueError) as error:
                errors.append(
                    CsvRowError(
                        row=row_number,
                        sku=str(raw.get("sku", "")).strip() or None,
                        code="CSV_ROW_INVALID",
                        message=_safe_validation_message(error),
                    )
                )
                continue
            previous = accepted.get(parsed.sku)
            if previous is None and parsed.sku not in conflicted:
                accepted[parsed.sku] = parsed
                first_rows[parsed.sku] = row_number
                continue
            if previous is not None and _configuration(previous) == _configuration(parsed):
                continue
            conflicted.add(parsed.sku)
            accepted.pop(parsed.sku, None)
            errors.append(
                CsvRowError(
                    row=row_number,
                    sku=parsed.sku,
                    code="CSV_DUPLICATE_CONFLICT",
                    message=(
                        "Duplicate SKU conflicts with row "
                        f"{first_rows.get(parsed.sku, row_number)} and was excluded."
                    ),
                )
            )
    except csv.Error:
        raise CsvFileError("CSV structure is invalid") from None
    return CsvSelection(
        rows=tuple(accepted.values()),
        errors=tuple(errors),
        input_row_count=input_row_count,
    )


def _optional_float(raw: str | None) -> float | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError("ratio must be numeric") from None


def _configuration(row: CsvSelectionRow) -> tuple[object, ...]:
    return row.fulfillment_mode, row.fba_ratio, row.fbm_ratio


def _safe_validation_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        first = error.errors(include_url=False)[0]
        return str(first.get("msg", "CSV row is invalid"))[:1024]
    return str(error)[:1024]
