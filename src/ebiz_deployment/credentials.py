"""Deployment-approved environment secret names."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType


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


__all__ = ["EnvironmentSecretResolver"]
