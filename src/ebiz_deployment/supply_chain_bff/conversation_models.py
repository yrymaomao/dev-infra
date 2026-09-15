"""BFF-owned conversation persistence. No Runtime ORM dependencies."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .models import SCHEMA, Base


class Conversation(Base):
    __tablename__ = "openclaw_conversation"
    __table_args__ = {"schema": SCHEMA}
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
