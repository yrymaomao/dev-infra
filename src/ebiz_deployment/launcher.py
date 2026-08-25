"""Production launcher that injects deployment-owned providers into Runtime."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from agent_runtime.application.provider_composition import ProviderCompositionRoot
from agent_runtime.cli import api as runtime_api

from .composition import build_provider_composition
from .config import load_deployment_config


class RuntimeMain(Protocol):
    def __call__(
        self,
        argv: list[str] | None = None,
        *,
        provider_composition: ProviderCompositionRoot | None = None,
    ) -> int: ...


def launch(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runtime_main: RuntimeMain = runtime_api.main,
) -> int:
    """Validate all deployment-owned inputs before handing control to Runtime."""

    if sys.dont_write_bytecode is not True:
        raise ValueError("Python bytecode writes must be disabled before deployment startup")
    environment = os.environ if environ is None else environ
    config_path = environment.get("EBIZ_DEPLOYMENT_CONFIG", "").strip()
    if not config_path:
        raise ValueError("EBIZ_DEPLOYMENT_CONFIG is required")
    config = load_deployment_config(Path(config_path), environment)
    runtime_policy_path = environment.get("APP_PLUGIN_POLICY_PATH", "").strip()
    if not runtime_policy_path or Path(runtime_policy_path).resolve() != (
        config.runtime.plugin_policy_path.resolve()
    ):
        raise ValueError("APP_PLUGIN_POLICY_PATH must match runtime.plugin_policy_path")
    artifacts = build_provider_composition(config, dict(environment))
    return runtime_main(
        list(argv) if argv is not None else None, provider_composition=artifacts.root
    )


def main(argv: list[str] | None = None) -> int:
    try:
        return launch(argv)
    except Exception as error:
        print(f"Deployment preflight failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["launch", "main"]
