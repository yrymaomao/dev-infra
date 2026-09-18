"""BFF-owned reception persistence. No Runtime ORM dependencies.

The three conversation tables and the run-binding table are shared by every
reception profile; rows are told apart by ``profile_version`` (conversations)
and by the agent prefix of ``session_key`` (run bindings). Migration 0009 adds
the ``(profile_version, tenant_id)`` index that every owner lookup uses.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ebiz_deployment.bff_persistence import SCHEMA, Base

RECEPTION_SCHEMA = SCHEMA


class Conversation(Base):
    __tablename__ = "openclaw_conversation"
    __table_args__ = (
        Index("ix_openclaw_conversation_profile_tenant", "profile_version", "tenant_id"),
        {"schema": SCHEMA},
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(256))
    principal_id: Mapped[str] = mapped_column(String(256))
    profile_version: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationTurn(Base):
    __tablename__ = "openclaw_conversation_turn"
    __table_args__ = (
        UniqueConstraint("conversation_id", "request_id", name="uq_chat_turn_request"),
        {"schema": SCHEMA},
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey(f"{SCHEMA}.openclaw_conversation.id"))
    request_id: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    prompt_ref: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), default="accepted", index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationEvent(Base):
    __tablename__ = "openclaw_conversation_event"
    __table_args__ = {"schema": SCHEMA}
    turn_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.openclaw_conversation_turn.id"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload_ref: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))


class OpenClawRunBinding(Base):
    __tablename__ = "openclaw_run_binding"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_key", name="uq_openclaw_run_session"),
        Index("ix_openclaw_run_active", "tenant_id", "active", "expires_at"),
        {"schema": SCHEMA},
    )

    run_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    turn_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(256), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_key: Mapped[str] = mapped_column(String(256), nullable=False)
    selector: Mapped[str] = mapped_column(String(256), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


__all__ = [
    "RECEPTION_SCHEMA",
    "Conversation",
    "ConversationEvent",
    "ConversationTurn",
    "OpenClawRunBinding",
]
