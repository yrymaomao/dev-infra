"""Request contracts of the Adapter- and Gateway-facing connector routes."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OpenClawTurnRequest(StrictModel):
    prompt: str = Field(min_length=1, max_length=8192)
    client_request_id: UUID = Field(strict=False)


class OpenClawCredentialRequest(StrictModel):
    selector: str = Field(min_length=1, max_length=256)
    run_id: str = Field(alias="runId", min_length=1, max_length=512)


class OpenClawEndRunRequest(StrictModel):
    run_id: str = Field(alias="runId", min_length=1, max_length=512)


class OpenClawPolicyCheckRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(min_length=1, max_length=256)
    session_key: str = Field(min_length=1, max_length=256)
    action: Literal["discover", "describe", "invoke", "status", "result", "cancel"]
    candidate_offer_ids: list[str] = Field(max_length=256)


__all__ = [
    "OpenClawCredentialRequest",
    "OpenClawEndRunRequest",
    "OpenClawPolicyCheckRequest",
    "OpenClawTurnRequest",
]
