"""Deployment secret and short-lived request credential resolvers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

_OPAQUE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class EnvironmentSecretResolver:
    """Resolve only deployment-approved symbolic names from a fixed environment view."""

    def __init__(self, allowed_env: Mapping[str, str], environ: Mapping[str, str]) -> None:
        self._allowed_env = MappingProxyType(dict(allowed_env))
        self._environ = environ

    def __repr__(self) -> str:
        return f"EnvironmentSecretResolver(symbols={tuple(sorted(self._allowed_env))!r})"

    async def resolve(self, secret_name: str) -> str:
        environment_name = self._allowed_env.get(secret_name)
        if environment_name is None:
            raise ValueError("secret name is not in the deployment allowlist")
        value = self._environ.get(environment_name, "")
        if not value:
            raise ValueError(f"secret {secret_name!r} is unavailable")
        return value

    def require_available(self, secret_names: Sequence[str]) -> None:
        for secret_name in secret_names:
            environment_name = self._allowed_env.get(secret_name)
            if environment_name is None or not self._environ.get(environment_name, ""):
                raise ValueError(f"secret {secret_name!r} is unavailable")


class _BrokerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1, max_length=128)
    access_token: str = Field(min_length=1, max_length=16384)
    expires_at: AwareDatetime


class HttpsCredentialBrokerResolver:
    """Resolve every opaque handle with a new authenticated broker request."""

    def __init__(
        self,
        *,
        url: str,
        auth_secret_name: str,
        allowed_provider_ids: Sequence[str],
        timeout_seconds: float,
        secret_resolver: EnvironmentSecretResolver,
    ) -> None:
        parsed = urlsplit(url)
        is_loopback = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        if not parsed.netloc or (parsed.scheme != "https" and not is_loopback):
            raise ValueError("credential broker must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credential broker URL must not contain credentials")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("credential broker timeout is invalid")
        self._url = url
        self._auth_secret_name = auth_secret_name
        self._allowed_provider_ids = frozenset(allowed_provider_ids)
        self._timeout_seconds = timeout_seconds
        self._secret_resolver = secret_resolver

    def __repr__(self) -> str:
        providers = tuple(sorted(self._allowed_provider_ids))
        return (
            "HttpsCredentialBrokerResolver("
            f"providers={providers!r}, timeout_seconds={self._timeout_seconds!r})"
        )

    async def resolve(self, credential_ref: str, *, provider_id: str) -> str:
        if provider_id not in self._allowed_provider_ids:
            raise ValueError("credential provider is not allowlisted")
        if _OPAQUE_REF.fullmatch(credential_ref) is None:
            raise ValueError("credential reference is malformed")
        broker_authorization = await self._secret_resolver.resolve(self._auth_secret_name)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._url,
                    json={"credential_ref": credential_ref, "provider_id": provider_id},
                    headers={
                        "Authorization": f"Bearer {broker_authorization}",
                        "Accept": "application/json",
                    },
                )
            response.raise_for_status()
            parsed = _BrokerResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError, ValidationError):
            raise ValueError("credential broker resolution failed") from None
        if parsed.provider_id != provider_id or parsed.expires_at <= datetime.now(UTC):
            raise ValueError("credential broker resolution failed")
        return parsed.access_token


__all__ = ["EnvironmentSecretResolver", "HttpsCredentialBrokerResolver"]
