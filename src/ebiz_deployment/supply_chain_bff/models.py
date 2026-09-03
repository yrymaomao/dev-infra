"""Independent Supply Chain BFF persistence model; never imports Runtime ORM."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "supply_chain_bff"


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "agent_execution_batch"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_batch_tenant_id"),
        UniqueConstraint("tenant_id", "client_request_id", name="uq_batch_tenant_request"),
        CheckConstraint("sku_count BETWEEN 1 AND 200", name="ck_batch_sku_count"),
        CheckConstraint("completed_count BETWEEN 0 AND sku_count", name="ck_batch_completed"),
        CheckConstraint("failed_count BETWEEN 0 AND sku_count", name="ck_batch_failed"),
        CheckConstraint("eta_low_seconds >= 0", name="ck_batch_eta_low"),
        CheckConstraint("eta_high_seconds >= eta_low_seconds", name="ck_batch_eta_order"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACCEPTED")
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runtime_mode: Mapped[str | None] = mapped_column(String(32))
    marketplace: Mapped[str] = mapped_column(String(8), nullable=False, default="US")
    fulfillment_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="FBM")
    snapshot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    eta_profile_version: Mapped[str] = mapped_column(String(128), nullable=False)
    eta_low_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    eta_high_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    eta_ewma_seconds: Mapped[float | None] = mapped_column()
    eta_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_ref: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BatchItem(Base):
    __tablename__ = "agent_execution_batch_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            [f"{SCHEMA}.agent_execution_batch.tenant_id", f"{SCHEMA}.agent_execution_batch.id"],
            name="fk_batch_item_batch",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "batch_id", "item_index", name="uq_batch_item_index"),
        UniqueConstraint(
            "tenant_id", "runtime_execution_id", name="uq_batch_item_runtime_execution"
        ),
        CheckConstraint("item_index >= 0", name="ck_batch_item_index"),
        CheckConstraint("attempt_count >= 0", name="ck_batch_item_attempt"),
        Index("ix_batch_item_dispatch", "status", "next_attempt_at", "lease_expires_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    batch_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sku_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_execution_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    runtime_session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    runtime_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    result_ref: Mapped[str | None] = mapped_column(Text)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    error_phase: Mapped[str | None] = mapped_column(String(32))
    error_category: Mapped[str | None] = mapped_column(String(32))
    error_retryable: Mapped[bool | None] = mapped_column(Boolean)
    safe_message: Mapped[str | None] = mapped_column(String(1024))
    request_id: Mapped[str | None] = mapped_column(String(256))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
    )


class BatchActivity(Base):
    __tablename__ = "agent_execution_batch_activity"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            [f"{SCHEMA}.agent_execution_batch.tenant_id", f"{SCHEMA}.agent_execution_batch.id"],
            name="fk_batch_activity_batch",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "batch_id", "event_key", name="uq_batch_activity_event"),
        Index("ix_batch_activity_replay", "tenant_id", "batch_id", "created_at", "id"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    stream_sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(), nullable=False, unique=True
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    batch_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    item_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    execution_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    execution_sequence: Mapped[int | None] = mapped_column(BigInteger)
    event_key: Mapped[str] = mapped_column(String(256), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    progress_current: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    safe_message: Mapped[str] = mapped_column(String(1024), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(128))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
