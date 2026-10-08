"""Materialize validated chat results as ordinary, owner-bound read-only reports.

No outbox entry is created: the Workflow has already executed. Report rows and
the completed operation event commit together; a replay cannot dispatch a batch.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ebiz_deployment.openclaw_reception.conversation_repository import ConversationRepository
from ebiz_deployment.openclaw_reception.models import Conversation, ConversationTurn

from .conversation_result import validated_analysis_artifact
from .level2_models import ReportBatch, ReportRun


@dataclass(frozen=True)
class PreparedReport:
    tenant: str
    artifact: dict[str, Any]
    staged: Any
    operation_id: str
    execution_id: UUID


class SupplyChainConversationRepository(ConversationRepository):
    def __init__(
        self, *args: Any, credential_reference: Callable[[str, str], str] | None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._credential_reference = credential_reference

    async def _prepare_result(
        self, tenant: str, result: object, event: dict[str, Any]
    ) -> PreparedReport:
        artifact = await validated_analysis_artifact(self, tenant, result)
        if artifact["item_offset"] != 0:
            raise ValueError("Chat report offset must be zero")
        staged = await self._stage(tenant, artifact)
        await self._commit_staged(tenant, staged)
        return PreparedReport(
            tenant,
            artifact,
            staged,
            event["payload"]["operation_id"],
            UUID(event["payload"]["execution_id"]),
        )

    async def _commit_result(
        self, session: AsyncSession, turn: ConversationTurn, publication: object
    ) -> None:
        if not isinstance(publication, PreparedReport):
            raise ValueError("Invalid chat report publication")
        conversation = await session.get(Conversation, turn.conversation_id)
        if (
            conversation is None
            or conversation.tenant_id != publication.tenant
            or conversation.profile_version != self._profile.profile_version
        ):
            raise LookupError("Chat report owner unavailable")
        report_id = UUID(publication.artifact["report_run_id"])
        batch_id = UUID(publication.artifact["batch_id"])
        owner = conversation.principal_id if self._credential_reference is not None else None
        previous = await session.get(ReportRun, report_id)
        if previous is not None:
            batch = await session.get(ReportBatch, batch_id)
            if (
                previous.tenant_id != publication.tenant
                or previous.owner_uid != owner
                or batch is None
                or batch.report_run_id != report_id
                or batch.runtime_execution_id != publication.execution_id
                or batch.result_hash != publication.staged.payload_hash
            ):
                raise ValueError("Chat report identity conflict")
            return
        items = publication.artifact["items"]
        counts = {
            status: sum(item["result_status"] == status for item in items)
            for status in ("COMPLETE", "BLOCKED", "FAILED")
        }
        now = datetime.now(UTC)
        cutoffs = {item["forecast"]["data_cutoff"] for item in items if item.get("forecast")}
        cutoff = datetime.fromisoformat(next(iter(cutoffs))) if len(cutoffs) == 1 else None
        status = (
            "FAILED"
            if counts["FAILED"] == len(items)
            else "PARTIAL"
            if counts["FAILED"]
            else "SUCCEEDED"
        )
        reference = (
            self._credential_reference(publication.tenant, conversation.principal_id)
            if self._credential_reference is not None
            else None
        )
        session.add(
            ReportRun(
                id=report_id,
                tenant_id=publication.tenant,
                owner_uid=owner,
                credential_ref=reference,
                client_request_id=f"chat:{turn.id}:{publication.operation_id}",
                policy_mode="ACTIVE_AT_RUN",
                report_schema_version="supply-chain.report.v2",
                data_cutoff=cutoff,
                status=status,
                sku_count=len(items),
                batch_count=1,
                complete_count=counts["COMPLETE"],
                blocked_count=counts["BLOCKED"],
                failed_count=counts["FAILED"],
                next_attempt_at=now,
                completed_at=now,
                risk_flags=sorted({flag for item in items for flag in item.get("risk_flags", [])}),
            )
        )
        await session.flush()
        session.add(
            ReportBatch(
                id=batch_id,
                tenant_id=publication.tenant,
                report_run_id=report_id,
                batch_no=1,
                item_offset=0,
                item_count=len(items),
                status="SUCCEEDED",
                complete_count=counts["COMPLETE"],
                blocked_count=counts["BLOCKED"],
                failed_count=counts["FAILED"],
                next_attempt_at=now,
                completed_at=now,
                runtime_execution_id=publication.execution_id,
                result_ref=publication.staged.payload_ref,
                result_hash=publication.staged.payload_hash,
            )
        )
