"""Dev-only same-host transport. No credentials, Broker, cache or business logic.

Run only in the dedicated application network namespace. Ports are bound to
literal loopback; targets and paths cannot be supplied by an HTTP caller.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import ipaddress
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

HOST = "192.168.10.18"
KEY_PATH = "/erp/feign/mcp/key/query/aggr-key"
MCP_UPSTREAM_PATH = "/erp/aggr-mcp"
NAMING_PATH = "/nacos/v1/ns/instance/list"
LOGIN_PATH = "/nacos/v1/auth/login"
MAX_BODY = 8 * 1024 * 1024
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def rewrite_discovery(body: object) -> dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("hosts"), list):
        raise ValueError("Invalid discovery response")
    result = copy.deepcopy(body)
    result["hosts"] = []
    for source in body["hosts"]:
        if not isinstance(source, dict):
            continue
        weight = source.get("weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool):
            continue
        metadata = source.get("metadata", {})
        if (
            source.get("ip") != HOST
            or type(source.get("port")) is not int
            or source["port"] != 9249
            or source.get("healthy") is not True
            or source.get("enabled") is not True
            or not math.isfinite(weight)
            or weight <= 0
            or not isinstance(metadata, dict)
            or metadata.get("protocol", "http") != "http"
        ):
            continue
        target = copy.deepcopy(source)
        target.update(ip="127.0.0.1", port=18249)
        target["metadata"] = {**metadata, "protocol": "http"}
        result["hosts"].append(target)
    return result


def forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    blocked = HOP_HEADERS | {v.strip().lower() for v in headers.get("connection", "").split(",")}
    return {key: value for key, value in headers.items() if key.lower() not in blocked}


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(kind: str, injected_client: httpx.AsyncClient | None = None) -> FastAPI:
    if kind not in {"nacos", "mcp"}:
        raise ValueError("Unknown transport")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=5, read=300),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if injected_client is not None:
        app.state.client = injected_client

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"]
    )
    async def relay(request: Request) -> Response:
        if request.client is None or not is_loopback(request.client.host):
            return Response(status_code=403)
        path = request.url.path
        # Reject aliases rather than normalizing attacker-controlled paths.
        raw_path = request.scope.get("raw_path", b"").split(b"?", 1)[0]
        if b"%" in raw_path or b"\\" in raw_path or "//" in path:
            return Response(status_code=404)
        method = request.method
        naming = kind == "nacos" and path == NAMING_PATH and method == "GET"
        login = kind == "nacos" and path == LOGIN_PATH and method == "POST"
        key_query = kind == "mcp" and path == KEY_PATH and method == "POST"
        mcp = kind == "mcp" and path == "/mcp" and method in {"GET", "POST", "DELETE"}
        if not (naming or login or key_query or mcp):
            return Response(status_code=404)
        if naming and (
            request.query_params.getlist("serviceName")
            not in [["McpServer"], ["DEFAULT_GROUP@@McpServer"]]
        ):
            return Response(status_code=403)
        if mcp and not request.headers.get("x-mcp-key", "").strip():
            return Response(status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return Response(status_code=413)
        headers = forward_headers(request.headers)
        headers["accept-encoding"] = "identity"
        origin = f"http://{HOST}:{8848 if kind == 'nacos' else 9249}"
        target = origin + (MCP_UPSTREAM_PATH if mcp else path)
        if request.url.query:
            target += "?" + request.url.query
        client = app.state.client
        try:
            upstream = await client.send(
                client.build_request(method, target, content=bytes(body), headers=headers),
                stream=True,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return Response(status_code=502)
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            return Response(status_code=502)
        if naming:
            try:
                await upstream.aread()
                if upstream.status_code != 200:
                    return Response(status_code=502)
                return JSONResponse(rewrite_discovery(upstream.json()))
            except (ValueError, httpx.HTTPError):
                return Response(status_code=502)
            finally:
                await upstream.aclose()

        async def chunks() -> AsyncIterator[bytes]:
            try:
                if upstream.is_stream_consumed:
                    yield upstream.content
                else:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
            finally:
                await upstream.aclose()

        # No body logging, retries, credential rewriting, buffering or caching.
        return StreamingResponse(
            chunks(), status_code=upstream.status_code, headers=forward_headers(upstream.headers)
        )

    return app


async def mq_tunnel() -> None:
    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if not peer or not is_loopback(peer[0]):
            writer.close()
            await writer.wait_closed()
            return
        remote_writer = None
        tasks = []
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(HOST, 5672), timeout=5
            )

            async def pump(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            tasks = [
                asyncio.create_task(pump(reader, remote_writer)),
                asyncio.create_task(pump(remote_reader, writer)),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError):
            pass  # Close transport; the AMQP owner observes and handles failure.
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            if remote_writer is not None:
                remote_writer.close()
                await remote_writer.wait_closed()
            await writer.wait_closed()

    server = await asyncio.start_server(connection, "127.0.0.1", 18672, limit=65536)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("transport", choices=("nacos", "mcp", "mq"))
    args = parser.parse_args()
    if args.transport == "mq":
        asyncio.run(mq_tunnel())
    else:
        uvicorn.run(
            create_app(args.transport),
            host="127.0.0.1",
            port=18848 if args.transport == "nacos" else 18249,
            access_log=False,
            log_level="warning",
        )
