"""Additive Level 2 persistence models; SKU plaintext remains in the payload store."""

from __future__ import annotations

from datetime import datetime, time
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
from sqlalchemy.orm import Mapped, mapped_column

from .models import SCHEMA, Base


class SelectionPreview(Base):
    __tablename__ = "selection_preview"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_selection_preview_tenant_id"),
        UniqueConstraint(
            "tenant_id", "client_request_id", name="uq_selection_preview_tenant_request"
        ),
        CheckConstraint("matched_count BETWEEN 0 AND 10001", name="ck_selection_match_count"),
        CheckConstraint("attempt_count >= 0", name="ck_selection_attempt"),
        Index(
            "ix_selection_preview_work",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        Index("ix_selection_preview_expiry", "status", "expires_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    selector: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source_snapshot_id: Mapped[str | None] = mapped_column(String(256))
    snapshot_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_ref: Mapped[str | None] = mapped_column(Text)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    row_errors_ref: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    ambiguities: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class PolicyVersion(Base):
    __tablename__ = "policy_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_policy_version_tenant_id"),
        UniqueConstraint("tenant_id", "version", name="uq_policy_version_number"),
        UniqueConstraint("tenant_id", "digest", name="uq_policy_version_digest"),
        Index(
            "uq_policy_version_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("active"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    document_ref: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    warnings: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportSchedule(Base):
    __tablename__ = "report_schedule"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_report_schedule_tenant_id"),
        Index("ix_report_schedule_due", "active", "next_fire_at_utc"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    local_time: Mapped[time] = mapped_column(nullable=False, default=time(hour=12))
    selection_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    selector: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    fixed_skus_ref: Mapped[str | None] = mapped_column(Text)
    fixed_skus_hash: Mapped[str | None] = mapped_column(String(64))
    policy_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE_AT_RUN")
    policy_version: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_fire_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
    )


class ReportRun(Base):
    __tablename__ = "report_run"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_report_run_tenant_id"),
        UniqueConstraint("tenant_id", "client_request_id", name="uq_report_run_request"),
        UniqueConstraint(
            "tenant_id",
            "schedule_id",
            "scheduled_fire_at_utc",
            name="uq_report_run_scheduled_fire",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "selection_preview_id"],
            [f"{SCHEMA}.selection_preview.tenant_id", f"{SCHEMA}.selection_preview.id"],
            name="fk_report_run_selection",
        ),
        CheckConstraint("sku_count BETWEEN 0 AND 10000", name="ck_report_run_sku_count"),
        CheckConstraint("batch_count BETWEEN 0 AND 50", name="ck_report_run_batch_count"),
        CheckConstraint("complete_count BETWEEN 0 AND sku_count", name="ck_report_run_complete"),
        CheckConstraint("blocked_count BETWEEN 0 AND sku_count", name="ck_report_run_blocked"),
        CheckConstraint("failed_count BETWEEN 0 AND sku_count", name="ck_report_run_failed"),
        Index("ix_report_run_status", "tenant_id", "status", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    selection_preview_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    schedule_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    scheduled_fire_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    policy_snapshot_ref: Mapped[str | None] = mapped_column(Text)
    data_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACCEPTED")
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_count: Mapped[int] = mapped_column(Integer, nullable=False)
    complete_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_flags: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class ReportBatch(Base):
    __tablename__ = "report_batch"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "report_run_id"],
            [f"{SCHEMA}.report_run.tenant_id", f"{SCHEMA}.report_run.id"],
            name="fk_report_batch_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_report_batch_tenant_id"),
        UniqueConstraint("tenant_id", "report_run_id", "batch_no", name="uq_report_batch_no"),
        UniqueConstraint("tenant_id", "runtime_execution_id", name="uq_report_batch_execution"),
        CheckConstraint("batch_no BETWEEN 1 AND 50", name="ck_report_batch_no"),
        CheckConstraint("item_count BETWEEN 1 AND 200", name="ck_report_batch_item_count"),
        CheckConstraint("item_offset >= 0", name="ck_report_batch_offset"),
        CheckConstraint("attempt_count >= 0", name="ck_report_batch_attempt"),
        Index("ix_report_batch_dispatch", "status", "next_attempt_at", "lease_expires_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    report_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    batch_no: Mapped[int] = mapped_column(Integer, nullable=False)
    item_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    complete_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_execution_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    runtime_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    selection_payload_ref: Mapped[str | None] = mapped_column(Text)
    selection_payload_hash: Mapped[str | None] = mapped_column(String(64))
    result_ref: Mapped[str | None] = mapped_column(Text)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    safe_error: Mapped[dict[str, object] | None] = mapped_column(JSONB)
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


class ReportOutbox(Base):
    __tablename__ = "report_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            [f"{SCHEMA}.report_batch.tenant_id", f"{SCHEMA}.report_batch.id"],
            name="fk_report_outbox_batch",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "event_id", name="uq_report_outbox_event"),
        Index("ix_report_outbox_publish", "status", "next_attempt_at", "lease_expires_at"),
        {"schema": SCHEMA},
    )

    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    report_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    batch_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class ReportInbox(Base):
    __tablename__ = "report_inbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", name="uq_report_inbox_event"),
        {"schema": SCHEMA},
    )

    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class ReportActivity(Base):
    __tablename__ = "report_activity"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "report_run_id"],
            [f"{SCHEMA}.report_run.tenant_id", f"{SCHEMA}.report_run.id"],
            name="fk_report_activity_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "report_run_id", "event_key", name="uq_report_activity"),
        Index("ix_report_activity_replay", "tenant_id", "report_run_id", "stream_sequence"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    stream_sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(), nullable=False, unique=True
    )
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    report_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    batch_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    event_key: Mapped[str] = mapped_column(String(256), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_message: Mapped[str] = mapped_column(String(1024), nullable=False)
    progress_current: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
