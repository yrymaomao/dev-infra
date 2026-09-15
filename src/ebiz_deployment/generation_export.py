"""Per-instance deployment-generation export (design 2026-09-16 §12.2).

Runtime's ``export_gateway_generation`` exports exactly one
``GatewayCatalogGeneration`` through the public Registry projector; it performs
no file IO and no signing. This module is the controlled deployment job around
it: it takes the deployment's one Gateway composition, exports one payload per
OpenClaw host instance - each containing only that instance's offers under that
instance's own generation id - and writes the returned bytes once, never
overwriting. Signing (``sign-generation.mjs``) and manifest materialization
(``prepare-plugin-manifest.mjs``) stay with the Adapter package; the SHA-256
printed here is the ``expectedPayloadDigest`` the operator pins in the
instance's trust JSON.

The single-instance path is unchanged: a composition with one instance exports
the same bytes it exported before instances existed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_runtime.application.tool_gateway_export import export_gateway_generation
from agent_runtime.application.tool_gateway_projection import GatewayCatalogGeneration

from .tool_gateway import SharedToolGatewayComposition

_DATABASE_URL_ENV = "AGENT_RUNTIME_DATABASE_URL"
_AUDIENCE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class GenerationExportError(RuntimeError):
    """The export refused; the message never carries database or Registry details."""


class OfferProjector(Protocol):
    """The part of ``RegistryToolOfferProjector`` the Runtime exporter calls."""

    async def project_exact(self, generation: GatewayCatalogGeneration, pin: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class InstanceGenerationExport:
    """One instance's unsigned generation bytes and the facts an operator pins."""

    agent_id: str
    generation_id: str
    catalog_revision: str
    tool_names: tuple[str, ...]
    payload: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def filename(self) -> str:
        return f"generation-{self.agent_id}.json"

    def receipt(self, path: Path | None = None) -> dict[str, Any]:
        document: dict[str, Any] = {
            "agent_id": self.agent_id,
            "generation_id": self.generation_id,
            "catalog_revision": self.catalog_revision,
            "tool_names": list(self.tool_names),
            "expected_payload_digest": self.digest,
            "bytes": len(self.payload),
        }
        if path is not None:
            document["path"] = str(path)
        return document


def _check_payload(instance_agent: str, expected: GatewayCatalogGeneration, payload: bytes) -> None:
    """The exported bytes must name exactly the instance's tools under its own id."""

    document = json.loads(payload)
    names = [offer["name"] for offer in document["offers"]]
    if (
        document["generation_id"] != expected.generation_id
        or document["catalog_revision"] != expected.catalog_revision
        or document["tenant_id"] != expected.tenant_id
        or sorted(names) != sorted(pin.name for pin in expected.pins)
        or any(
            offer["reference"]["generation_id"] != expected.generation_id
            for offer in document["offers"]
        )
    ):
        raise GenerationExportError(
            f"exported generation for instance {instance_agent!r} does not match its pins"
        )


async def export_instance_generations(
    composition: SharedToolGatewayComposition,
    *,
    projector: OfferProjector,
    deployment_audience: str,
) -> tuple[InstanceGenerationExport, ...]:
    """One unsigned payload per OpenClaw host instance, each with only its offers."""

    exports = []
    for instance in composition.instances:
        payload = await export_gateway_generation(
            generation=instance.generation,
            projector=projector,  # type: ignore[arg-type]
            deployment_audience=deployment_audience,
        )
        _check_payload(instance.agent_id, instance.generation, payload)
        exports.append(
            InstanceGenerationExport(
                agent_id=instance.agent_id,
                generation_id=instance.generation.generation_id,
                catalog_revision=instance.generation.catalog_revision,
                tool_names=instance.tool_names,
                payload=payload,
            )
        )
    return tuple(exports)


async def export_generation(
    composition: SharedToolGatewayComposition,
    *,
    projector: OfferProjector,
    deployment_audience: str,
) -> bytes:
    """The single-instance export: today's one payload, byte for byte."""

    generation = composition.generation  # refuses a multi-instance composition
    payload = await export_gateway_generation(
        generation=generation,
        projector=projector,  # type: ignore[arg-type]
        deployment_audience=deployment_audience,
    )
    _check_payload(composition.instances[0].agent_id, generation, payload)
    return payload


def write_exports(
    exports: Sequence[InstanceGenerationExport], out_dir: Path
) -> tuple[dict[str, Any], ...]:
    """Write every payload once (``xb``); an existing file is never overwritten."""

    out_dir.mkdir(parents=True, exist_ok=True)
    targets = [out_dir / export.filename for export in exports]
    for target in targets:
        if target.exists():
            raise GenerationExportError(
                f"{target.name} already exists; exports are never overwritten"
            )
    receipts = []
    for export, target in zip(exports, targets, strict=True):
        with open(target, "xb") as handle:
            handle.write(export.payload)
        receipts.append(export.receipt(target))
    return tuple(receipts)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebiz-gateway-generation-export",
        description=(
            "Export one unsigned Tool Gateway generation per OpenClaw host instance "
            "from the Registry; sign each with the Adapter's sign-generation.mjs"
        ),
    )
    parser.add_argument("--out-dir", required=True, help="new files generation-<agent>.json")
    parser.add_argument(
        "--audience",
        required=True,
        help="deployment_audience pinned by every instance's trust JSON",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _composition(environment: Mapping[str, str]) -> SharedToolGatewayComposition:
    from .launcher import build_gateway_composition, load_launch_config

    composition = build_gateway_composition(environment, load_launch_config(environment))
    if composition is None:
        raise GenerationExportError("no Tool Gateway offer is enabled in this environment")
    return composition


async def _run_against_database(
    arguments: argparse.Namespace, database_url: str, environment: Mapping[str, str]
) -> tuple[InstanceGenerationExport, ...]:
    from agent_runtime.application.tool_gateway_projection import RegistryToolOfferProjector
    from agent_runtime.db.unit_of_work import UnitOfWork
    from agent_runtime.registry.capabilities import PostgresCapabilityRegistry
    from agent_runtime.registry.workflows import PostgresWorkflowRegistry
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    composition = _composition(environment)
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    def unit_of_work_factory() -> UnitOfWork:
        return UnitOfWork(session_factory)

    try:
        return await export_instance_generations(
            composition,
            projector=RegistryToolOfferProjector(
                PostgresCapabilityRegistry(unit_of_work_factory),
                PostgresWorkflowRegistry(unit_of_work_factory),
            ),
            deployment_audience=arguments.audience,
        )
    finally:
        await engine.dispose()


Runner = Callable[[argparse.Namespace, str], tuple[InstanceGenerationExport, ...]]


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    database_url: str | None = None,
    run: Runner | None = None,
) -> int:
    """Export and write without exposing database or Registry exception details."""

    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    environment = os.environ if environ is None else environ
    if re.fullmatch(_AUDIENCE, arguments.audience) is None:
        print("generation export: FAIL --audience must be a gateway identifier", file=sys.stderr)
        return 2
    configured_url = (
        database_url if database_url is not None else environment.get(_DATABASE_URL_ENV)
    )
    if run is None and not configured_url:
        print("generation export: FAIL database is unavailable", file=sys.stderr)
        return 1
    try:
        if run is not None:
            exports = run(arguments, configured_url or "")
        else:
            from agent_runtime.event_loop import configure_psycopg_event_loop_policy

            configure_psycopg_event_loop_policy()
            exports = asyncio.run(
                _run_against_database(arguments, configured_url or "", environment)
            )
        receipts = write_exports(exports, Path(arguments.out_dir))
    except GenerationExportError as error:
        print(f"generation export: FAIL {error}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never expose raw database/Registry details
        print("generation export: FAIL internal export error", file=sys.stderr)
        return 1
    document = {"status": "PASS", "instances": list(receipts)}
    if arguments.format == "json":
        print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    else:
        print("generation export: PASS")
        for receipt in receipts:
            print(
                f"  {receipt['agent_id']}: {receipt['generation_id']} "
                f"tools={','.join(receipt['tool_names'])} "
                f"sha256={receipt['expected_payload_digest']} -> {receipt['path']}"
            )
    return 0


__all__ = [
    "GenerationExportError",
    "InstanceGenerationExport",
    "export_generation",
    "export_instance_generations",
    "main",
    "write_exports",
]
