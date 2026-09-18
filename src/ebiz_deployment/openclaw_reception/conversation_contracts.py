"""Authority for public conversation OpenAPI and generated frontend contracts."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    profile_version: str


class TurnAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    conversation_id: str


class OperationErrorView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error_code: str
    category: Literal[
        "validation",
        "authorization",
        "configuration",
        "transient",
        "permanent",
        "conflict",
    ]
    phase: Literal["validation", "routing", "invocation", "persistence", "governance", "recovery"]
    retryable: bool
    safe_message: str
    request_id: str


class OperationView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str
    state: str
    execution_id: str | None = None
    error: OperationErrorView | None = None


class TurnSnapshot(TurnAccepted):
    client_request_id: str
    message_id: str
    state: Literal["accepted", "running", "completed", "failed", "interrupted"]
    sequence: int = Field(ge=0)
    prompt: str
    reply: str
    operations: list[OperationView]
    analyses: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, str] | None
    cursor: str = ""


class ConversationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    turns: list[TurnSnapshot]


class TurnEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int = Field(ge=1)
    kind: Literal[
        "turn.accepted",
        "assistant.message.started",
        "assistant.text.delta",
        "assistant.text.replaced",
        "tool.started",
        "operation.updated",
        "assistant.message.completed",
        "turn.completed",
        "turn.failed",
    ]
    message_id: str
    payload: dict[str, Any]
