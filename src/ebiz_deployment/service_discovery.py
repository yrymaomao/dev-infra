"""Read-only Nacos naming discovery with bounded caches and checked egress."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import ssl
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

import nacos  # type: ignore[import-untyped]  # SDK 0.1.17 ships no typing marker.


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def normalize_nacos_addresses(value: str) -> str:
    """SDK 0.1.17 cannot parse IPv6; reject it before construction."""
    origins = []
    for address in value.split(","):
        try:
            if any(character.isspace() or ord(character) < 32 for character in address):
                raise ValueError()
            parsed = urlsplit(address if "://" in address else "http://" + address)
            if (
                not parsed.hostname
                or ":" in parsed.hostname
                or parsed.port is None
                or not 1 <= parsed.port <= 65535
            ):
                raise ValueError()
            origins.append(secure_service_origin(parsed.geturl(), (parsed.hostname,)))
        except ValueError:
            raise ValueError(
                "Nacos server_addresses require HTTPS or loopback HTTP origins"
            ) from None
    return ",".join(origins)


class _ReadOnlyNacosClient(nacos.NacosClient):  # type: ignore[misc]
    def initLog(self, logDir: object, log_level: object, log_rotation_backup_count: object) -> None:
        # SDK 0.1.17 logs access tokens and raw naming bodies. Never enable that
        # logger or its automatic home-directory file writer in this host.
        logging.getLogger("nacos.client").disabled = True

    def _do_sync_req(
        self,
        url: str,
        headers: Any = None,
        params: Any = None,
        data: Any = None,
        timeout: float | None = None,
        method: str = "GET",
        module: str = "config",
    ) -> Any:
        # Reuse SDK authentication/naming, but not its unverified TLS transport.
        # Only these two read-only/login requests are authorized in this host.
        if (module, method, url) not in {
            ("naming", "GET", "/nacos/v1/ns/instance/list"),
            ("login", "POST", "/nacos/v1/auth/login"),
        }:
            raise ValueError("Nacos request is not authorized")
        all_headers, all_params = dict(headers or {}), dict(params or {})
        self._inject_version_info(all_headers)
        self._inject_auth_info(all_headers, all_params, data, module)
        address, port = self.get_server()
        origin = normalize_nacos_addresses(f"{address}:{port}")
        query = "?" + urlencode(all_params) if all_params else ""
        request = Request(
            origin + url + query,
            headers=all_headers,
            method=method,
            data=urlencode(data).encode() if data else None,
        )
        opener = build_opener(
            ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), _NoRedirect()
        )
        return opener.open(request, timeout=timeout or self.default_timeout)


def create_nacos_client(
    addresses: str,
    *,
    namespace: str,
    username: str | None,
    password: str | None,
    timeout_seconds: float,
) -> Any:
    client = _ReadOnlyNacosClient(normalize_nacos_addresses(addresses), namespace=namespace)
    client.set_options(default_timeout=timeout_seconds, proxies={})
    # Defer synchronous login until naming discovery runs in its bounded thread.
    client.username, client.password = username, password
    return client


class ServiceResolver(Protocol):
    async def resolve(self, service_name: str, group_name: str) -> str: ...


def secure_service_origin(origin: str, allowed_hosts: tuple[str, ...]) -> str:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
        if (
            parsed.hostname not in allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
            or any(character.isspace() or ord(character) < 32 for character in origin)
            or (
                parsed.scheme != "https"
                and not (
                    parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                )
            )
        ):
            raise ValueError()
    except ValueError:
        raise ValueError("MCP service unavailable") from None
    return origin.rstrip("/")


class NacosServiceResolver:
    """Reuse ERP-ai's naming approach without importing its application package."""

    def __init__(
        self,
        client: Any,
        *,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not allowed_hosts or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("Nacos discovery settings are invalid")
        self._client = client
        self._allowed_hosts = allowed_hosts
        self._timeout = timeout_seconds
        self._clock = clock
        self._expires = 0.0
        self._origins: tuple[str, ...] = ()
        self._weights: tuple[float, ...] = ()
        self._group: str | None = None
        self._lock = asyncio.Lock()

    async def resolve(self, service_name: str, group_name: str) -> str:
        if service_name != "McpServer" or not group_name or len(group_name) > 128:
            raise ValueError("MCP service unavailable")
        async with self._lock:
            if self._clock() >= self._expires or group_name != self._group:
                # Never serve expired discovery after a failed refresh.
                self._origins = ()
                self._weights = ()
                self._expires = 0.0
                self._group = None
                try:
                    async with asyncio.timeout(self._timeout):
                        answer = await asyncio.to_thread(
                            self._client.list_naming_instance,
                            service_name,
                            group_name=group_name,
                            healthy_only=True,
                        )
                except Exception:
                    raise ValueError("MCP service unavailable") from None
                origins: list[str] = []
                weights: list[float] = []
                hosts = answer.get("hosts") if isinstance(answer, dict) else None
                if isinstance(hosts, list):
                    for instance in hosts:
                        candidate = self._candidate(instance)
                        if candidate is not None:
                            origin, weight = candidate
                            origins.append(origin)
                            weights.append(weight)
                if not origins:
                    raise ValueError("MCP service unavailable")
                self._origins, self._weights = tuple(origins), tuple(weights)
                self._group = group_name
                self._expires = self._clock() + 5.0
            return random.SystemRandom().choices(self._origins, weights=self._weights, k=1)[0]

    def _candidate(self, instance: object) -> tuple[str, float] | None:
        if (
            not isinstance(instance, dict)
            or instance.get("healthy") is not True
            or instance.get("enabled") is not True
        ):
            return None
        try:
            weight = float(instance.get("weight", 0))
            port = instance.get("port")
            ip = instance.get("ip")
            metadata = instance.get("metadata", {})
            if (
                not math.isfinite(weight)
                or weight <= 0
                or type(port) is not int
                or not 1 <= port <= 65535
                or not isinstance(ip, str)
                or not isinstance(metadata, dict)
            ):
                return None
            host = f"[{ip}]" if ":" in ip else ip
            protocol = metadata.get("protocol", "http")
            origin = secure_service_origin(f"{protocol}://{host}:{port}", self._allowed_hosts)
        except (ValueError, TypeError, OverflowError):
            return None
        return origin, weight
