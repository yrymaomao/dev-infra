"""OpenClaw run bindings: the one table the Gateway's current-policy reply rests on.

Moved verbatim out of the Level 2 repository so the CRM profile can bind runs
without owning a Level 2 repository. ``Level2Repository`` keeps delegating
wrappers, so its existing callers and test doubles still work.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import OpenClawRunBinding


class RunBindingConflict(RuntimeError):
    """The run id is already bound to a different identity or selector."""


class RunBindings(Protocol):
    async def exchange_openclaw_run(
        self,
        *,
        selector: str,
        run_id: str,
        tenant_id: str,
        principal_id: str,
        agent_id: str,
        now: datetime,
        model_session_id: str | None = None,
        turn_id: UUID | None = None,
    ) -> dict[str, str]: ...

    async def end_openclaw_run(self, *, run_id: str, now: datetime) -> bool: ...

    async def authorize_openclaw_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        session_key: str,
        now: datetime,
    ) -> tuple[bool, str]: ...


class RunBindingRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def exchange_openclaw_run(
        self,
        *,
        selector: str,
        run_id: str,
        tenant_id: str,
        principal_id: str,
        agent_id: str,
        now: datetime,
        model_session_id: str | None = None,
        turn_id: UUID | None = None,
    ) -> dict[str, str]:
        session_id = model_session_id or str(
            uuid5(NAMESPACE_URL, f"ebizhub:openclaw:session:{tenant_id}:{run_id}")
        )
        session_digest = hashlib.sha256(
            f"{tenant_id}\x1f{principal_id}\x1f{run_id}".encode()
        ).hexdigest()
        session_key = f"agent:{agent_id}:openclaw:{session_digest[:48]}"
        expires_at = now.astimezone(UTC) + timedelta(minutes=10)
        async with self._factory() as session, session.begin():
            binding = await session.scalar(
                select(OpenClawRunBinding)
                .where(OpenClawRunBinding.run_id == run_id)
                .with_for_update()
            )
            if binding is None:
                binding = OpenClawRunBinding(
                    run_id=run_id,
                    turn_id=turn_id,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    session_id=session_id,
                    session_key=session_key,
                    selector=selector,
                    active=True,
                    expires_at=expires_at,
                )
                session.add(binding)
            elif (
                not binding.active
                or binding.tenant_id != tenant_id
                or binding.principal_id != principal_id
                or binding.selector != selector
                or binding.session_id != session_id
                or binding.session_key != session_key
            ):
                raise RunBindingConflict("OpenClaw run binding is not reusable")
            else:
                binding.expires_at = expires_at
            await session.flush()
            return {
                "tenantId": binding.tenant_id,
                "principalId": binding.principal_id,
                "sessionId": binding.session_id,
                "sessionKey": binding.session_key,
                "runId": binding.run_id,
            }

    async def end_openclaw_run(self, *, run_id: str, now: datetime) -> bool:
        async with self._factory() as session, session.begin():
            binding = await session.scalar(
                select(OpenClawRunBinding)
                .where(OpenClawRunBinding.run_id == run_id)
                .with_for_update()
            )
            if binding is None or not binding.active:
                return False
            binding.active = False
            binding.expires_at = now.astimezone(UTC)
            binding.row_version += 1
            return True

    async def authorize_openclaw_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        session_key: str,
        now: datetime,
    ) -> tuple[bool, str]:
        async with self._factory() as session:
            binding = await session.scalar(
                select(OpenClawRunBinding).where(
                    OpenClawRunBinding.tenant_id == tenant_id,
                    OpenClawRunBinding.principal_id == principal_id,
                    OpenClawRunBinding.session_key == session_key,
                )
            )
        if binding is None:
            return False, "0"
        active = binding.active and binding.expires_at > now.astimezone(UTC)
        return active, str(binding.row_version)


__all__ = ["RunBindingConflict", "RunBindingRepository", "RunBindings"]
