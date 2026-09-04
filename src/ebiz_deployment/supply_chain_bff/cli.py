"""Production entrypoint for the independently deployable Supply Chain BFF."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

import httpx
import uvicorn
from agent_runtime.cli.application import build_payload_store
from agent_runtime.cli.configuration import DeploymentConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .app import BffContainer, create_app
from .config import BffSettings
from .cursor import CursorSigner
from .dispatcher import BatchCoordinator
from .level2_repository import Level2Repository
from .level2_worker import Level2Worker
from .migration import upgrade
from .report_mq import AioPikaReportBus
from .repository import BatchRepository
from .runtime_client import RuntimeClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the Supply Chain BFF")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--migrate-only", action="store_true")
    return parser


async def _serve(settings: BffSettings, *, host: str, port: int) -> None:
    deployment = DeploymentConfig.from_environment()
    payload_store = build_payload_store(deployment)
    await payload_store.ensure_ready()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        runtime = RuntimeClient(http, base_url=settings.runtime_url)
        repository = BatchRepository(
            factory,
            payload_store=payload_store,
            tenant_concurrency=settings.tenant_dispatch_concurrency,
            global_concurrency=settings.global_dispatch_concurrency,
            eta_profile=settings.eta_profile,
        )
        coordinator = BatchCoordinator(
            repository=repository,
            runtime=runtime,
            settings=settings,
        )
        level2_repository = Level2Repository(factory, payload_store=payload_store)
        report_bus = (
            AioPikaReportBus(
                url=settings.rabbitmq_url,
                exchange_name=settings.rabbitmq_exchange,
                queue_name=settings.rabbitmq_queue,
                routing_key=settings.rabbitmq_routing_key,
            )
            if settings.level2_mq_enabled and settings.rabbitmq_url is not None
            else None
        )
        level2_worker = (
            Level2Worker(
                repository=level2_repository,
                runtime=runtime,
                settings=settings,
                authorization_for_tenant=coordinator.authorization_for_tenant,
                bus=report_bus,
            )
            if settings.level2_enabled
            else None
        )
        app = create_app(
            BffContainer(
                settings=settings,
                repository=repository,
                runtime=runtime,
                coordinator=coordinator,
                cursor=CursorSigner(
                    settings.cursor_hmac_key,
                    ttl=timedelta(days=7),
                ),
                level2_repository=level2_repository,
                level2_worker=level2_worker,
            )
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                access_log=False,
                server_header=False,
                proxy_headers=False,
            )
        )
        try:
            await server.serve()
        finally:
            await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    settings = BffSettings.from_environment()
    if arguments.migrate_only:
        upgrade(settings.database_url)
        return 0
    asyncio.run(_serve(settings, host=arguments.host, port=arguments.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
