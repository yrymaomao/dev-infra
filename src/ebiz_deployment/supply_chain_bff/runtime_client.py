"""Version-tolerant client for Runtime 0.1.6 and the 0.1.5 fallback."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx


class RuntimeRequestError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        phase: str,
        category: str,
        retryable: bool,
        safe_message: str,
        request_id: str | None,
    ) -> None:
        super().__init__(safe_message)
        self.status_code = status_code
        self.error_code = error_code
        self.phase = phase
        self.category = category
        self.retryable = retryable
        self.safe_message = safe_message
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class RuntimeStartResult:
    mode: Literal["async", "sync-polling"]
    execution_id: str
    root_execution_id: str
    session_id: str | None
    snapshot: dict[str, Any]


class RuntimeClient:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str) -> None:
        if not base_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
            raise ValueError("Runtime URL must be loopback HTTP or HTTPS")
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def start(
        self,
        *,
        authorization: str,
        payload: Mapping[str, object],
        respond_async: bool = True,
    ) -> RuntimeStartResult:
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
        }
        if respond_async:
            headers["Prefer"] = "respond-async"
        try:
            response = await self._http.post(
                f"{self._base_url}/v1/agent-executions",
                headers=headers,
                json=dict(payload),
            )
        except httpx.HTTPError:
            raise self._transport_error() from None
        body = self._body(response)
        if response.status_code not in {201, 202}:
            raise self._error(response, body)
        execution_id = self._required_string(body, "execution_id")
        root_execution_id = self._required_string(body, "root_execution_id")
        if response.status_code == 202:
            session_id = self._required_string(body, "session_id")
            if body.get("status") not in {"CREATED", "RUNNING"}:
                raise ValueError("Runtime async response has an invalid status")
            mode: Literal["async", "sync-polling"] = "async"
        else:
            session_id = None
            if not isinstance(body.get("status"), str):
                raise ValueError("Runtime sync response has an invalid status")
            mode = "sync-polling"
        return RuntimeStartResult(
            mode=mode,
            execution_id=execution_id,
            root_execution_id=root_execution_id,
            session_id=session_id,
            snapshot=body,
        )

    async def get_execution(self, *, authorization: str, execution_id: str) -> dict[str, Any]:
        try:
            response = await self._http.get(
                f"{self._base_url}/v1/executions/{execution_id}",
                headers={"Authorization": authorization, "Accept": "application/json"},
            )
        except httpx.HTTPError:
            raise self._transport_error() from None
        body = self._body(response)
        if response.status_code != 200:
            raise self._error(response, body)
        if self._required_string(body, "execution_id") != execution_id:
            raise ValueError("Runtime returned a mismatched execution")
        return body

    async def cancel(self, *, authorization: str, execution_id: str) -> dict[str, Any]:
        try:
            response = await self._http.post(
                f"{self._base_url}/v1/executions/{execution_id}/cancel",
                headers={"Authorization": authorization, "Accept": "application/json"},
            )
        except httpx.HTTPError:
            raise self._transport_error() from None
        body = self._body(response)
        if response.status_code != 200:
            raise self._error(response, body)
        return body

    async def model_feedback(
        self,
        *,
        authorization: str,
        payload: Mapping[str, object],
    ) -> dict[str, Any]:
        try:
            response = await self._http.post(
                f"{self._base_url}/v1/model-feedback",
                headers={"Authorization": authorization, "Accept": "application/json"},
                json=dict(payload),
            )
        except httpx.HTTPError:
            raise self._transport_error() from None
        body = self._body(response)
        if response.status_code != 200:
            raise self._error(response, body)
        return body

    async def stream(
        self,
        *,
        authorization: str,
        sequences: Mapping[str, int],
    ) -> AsyncIterator[dict[str, Any]]:
        subscriptions = [
            {"execution_id": execution_id, "after_sequence": sequence}
            for execution_id, sequence in sorted(sequences.items())
        ]
        try:
            async with self._http.stream(
                "POST",
                f"{self._base_url}/v1/execution-events/stream",
                headers={"Authorization": authorization, "Accept": "text/event-stream"},
                json={"subscriptions": subscriptions},
                timeout=None,
            ) as response:
                if response.status_code != 200:
                    raw = await response.aread()
                    try:
                        body = cast(dict[str, Any], json.loads(raw))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        body = {}
                    raise self._error(response, body)
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line.removeprefix("data: "))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        yield cast(dict[str, Any], event)
        except httpx.HTTPError:
            raise self._transport_error() from None

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError:
            raise ValueError("Runtime returned a non-JSON response") from None
        if not isinstance(value, dict):
            raise ValueError("Runtime returned an invalid response body")
        return cast(dict[str, Any], value)

    @staticmethod
    def _required_string(body: Mapping[str, Any], name: str) -> str:
        value = body.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Runtime response omitted {name}")
        return value

    @staticmethod
    def _error(response: httpx.Response, body: Mapping[str, Any]) -> RuntimeRequestError:
        return RuntimeRequestError(
            status_code=response.status_code,
            error_code=str(body.get("error_code", "RUNTIME_REQUEST_FAILED"))[:128],
            phase=str(body.get("phase", "invocation")),
            category=str(body.get("category", "transient")),
            retryable=(response.status_code in {429, 503}) or bool(body.get("retryable", False)),
            safe_message=str(body.get("safe_message", "Supply chain runtime request failed")),
            request_id=(
                str(body["request_id"])
                if body.get("request_id") is not None
                else response.headers.get("x-request-id")
            ),
        )

    @staticmethod
    def _transport_error() -> RuntimeRequestError:
        return RuntimeRequestError(
            status_code=503,
            error_code="RUNTIME_TRANSPORT_UNAVAILABLE",
            phase="invocation",
            category="transient",
            retryable=True,
            safe_message="The Agent Runtime is temporarily unavailable.",
            request_id=None,
        )
