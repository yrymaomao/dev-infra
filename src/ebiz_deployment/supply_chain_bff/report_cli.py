"""Thin authenticated CLI for the durable Supply Chain v2 BFF."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .selection_csv import MAX_CSV_BYTES

_TERMINAL = frozenset({"SUCCEEDED", "SUCCEEDED_EMPTY", "PARTIAL", "FAILED", "CANCELLED"})


class CliError(RuntimeError):
    """Safe operator-facing failure without response bodies or credentials."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ebiz-supply-chain")
    commands = parser.add_subparsers(dest="area", required=True)

    report = commands.add_parser("report")
    report_commands = report.add_subparsers(dest="action", required=True)
    submit = report_commands.add_parser("submit")
    submit.add_argument("--csv", type=Path, required=True)
    submit.add_argument("--request-id", required=True)
    status = report_commands.add_parser("status")
    status.add_argument("report_id", type=UUID)
    wait = report_commands.add_parser("wait")
    wait.add_argument("report_id", type=UUID)
    wait.add_argument("--timeout-seconds", type=int, default=7200)
    wait.add_argument("--poll-seconds", type=float, default=2.0)

    schedule = commands.add_parser("schedule")
    schedule_commands = schedule.add_subparsers(dest="action", required=True)
    run_now = schedule_commands.add_parser("run-now")
    run_now.add_argument("schedule_id", type=UUID)
    run_now.add_argument("--request-id", required=True)
    schedule_commands.add_parser("dispatch-due")
    return parser


def _settings() -> tuple[str, str]:
    origin = os.environ.get("SUPPLY_CHAIN_BFF_URL", "").strip().rstrip("/")
    token = os.environ.get("SUPPLY_CHAIN_BFF_BEARER_TOKEN", "").strip()
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CliError("SUPPLY_CHAIN_BFF_URL is unavailable")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise CliError("SUPPLY_CHAIN_BFF_URL requires HTTPS outside loopback")
    if not token or len(token) > 8192 or any(character.isspace() for character in token):
        raise CliError("SUPPLY_CHAIN_BFF_BEARER_TOKEN is unavailable")
    return origin, token


def _response(response: httpx.Response) -> dict[str, Any]:
    if not response.is_success:
        raise CliError(f"BFF request failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise CliError("BFF returned an invalid JSON response") from error
    if not isinstance(payload, dict):
        raise CliError("BFF returned an invalid response shape")
    return payload


def _preview_ready(
    client: httpx.Client, preview_id: str, *, timeout_seconds: float = 120.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        preview = _response(client.get(f"/api/supply-chain/v2/selection-previews/{preview_id}"))
        status = preview.get("status")
        if status == "READY":
            return preview
        if status in {"REJECTED", "EXPIRED"}:
            raise CliError(f"selection preview ended as {status}")
        if time.monotonic() >= deadline:
            raise CliError("selection preview did not become ready before timeout")
        time.sleep(0.5)


def _submit(client: httpx.Client, csv_path: Path, request_id: str) -> dict[str, Any]:
    try:
        size = csv_path.stat().st_size
    except OSError as error:
        raise CliError("CSV file is not readable") from error
    if size <= 0 or size > MAX_CSV_BYTES:
        raise CliError("CSV file size is outside the supported range")
    with csv_path.open("rb") as source:
        accepted = _response(
            client.post(
                "/api/supply-chain/v2/selection-imports",
                data={"client_request_id": request_id},
                files={"file": (csv_path.name, source, "text/csv")},
            )
        )
    preview_id = accepted.get("preview_id")
    if not isinstance(preview_id, str):
        raise CliError("selection preview identifier is unavailable")
    preview = _preview_ready(client, preview_id)
    if int(preview.get("matched_count", 0)) < 1:
        raise CliError("selection preview contains no valid SKU")
    return _response(
        client.post(
            "/api/supply-chain/v2/report-runs",
            headers={"X-Request-ID": request_id},
            json={
                "selection_preview_id": preview_id,
                "policy_mode": "ACTIVE_AT_RUN",
                "policy_version": None,
                "client_request_id": request_id,
            },
        )
    )


def _wait(
    client: httpx.Client, report_id: UUID, *, timeout_seconds: int, poll_seconds: float
) -> dict[str, Any]:
    if timeout_seconds < 1 or not 0.1 <= poll_seconds <= 60:
        raise CliError("wait bounds are invalid")
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = _response(client.get(f"/api/supply-chain/v2/reports/{report_id}"))
        if result.get("status") in _TERMINAL:
            return result
        if time.monotonic() >= deadline:
            raise CliError("report did not reach terminal state before timeout")
        time.sleep(poll_seconds)


def _run(arguments: argparse.Namespace, client: httpx.Client) -> dict[str, Any]:
    if arguments.area == "report" and arguments.action == "submit":
        return _submit(client, arguments.csv, arguments.request_id)
    if arguments.area == "report" and arguments.action == "status":
        return _response(client.get(f"/api/supply-chain/v2/reports/{arguments.report_id}"))
    if arguments.area == "report" and arguments.action == "wait":
        return _wait(
            client,
            arguments.report_id,
            timeout_seconds=arguments.timeout_seconds,
            poll_seconds=arguments.poll_seconds,
        )
    if arguments.area == "schedule" and arguments.action == "run-now":
        return _response(
            client.post(
                f"/api/supply-chain/v2/schedules/{arguments.schedule_id}/run-now",
                headers={"Idempotency-Key": arguments.request_id},
            )
        )
    if arguments.area == "schedule" and arguments.action == "dispatch-due":
        return _response(client.post("/api/supply-chain/v2/schedules/dispatch-due"))
    raise CliError("unsupported command")


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        origin, token = _settings()
        with httpx.Client(
            base_url=origin,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        ) as client:
            result = _run(arguments, client)
    except (CliError, OSError) as error:
        _fail(str(error))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
