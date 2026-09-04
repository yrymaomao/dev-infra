"""Add durable Supply Chain Level 2 selection, report, schedule, policy, and MQ tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_supply_chain_level2"
down_revision = "0001_supply_chain_bff"
branch_labels = None
depends_on = None

SCHEMA = "supply_chain_bff"
UTC_NOW = sa.text("CURRENT_TIMESTAMP")
EMPTY_OBJECT = sa.text("'{}'::jsonb")
EMPTY_ARRAY = sa.text("'[]'::jsonb")


def _level2_tables() -> tuple[sa.Table, ...]:
    """Return a migration-local schema snapshot that cannot drift with the ORM."""
    metadata = sa.MetaData(schema=SCHEMA)

    selection_preview = sa.Table(
        "selection_preview",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("client_request_id", sa.String(200), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("selector", postgresql.JSONB(), nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("source_snapshot_id", sa.String(256)),
        sa.Column("snapshot_time", sa.DateTime(timezone=True)),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("payload_ref", sa.Text()),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("row_errors_ref", sa.Text()),
        sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("ambiguities", postgresql.JSONB(), nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(256)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.UniqueConstraint("tenant_id", "id", name="uq_selection_preview_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "client_request_id", name="uq_selection_preview_tenant_request"
        ),
        sa.CheckConstraint("matched_count BETWEEN 0 AND 10001", name="ck_selection_match_count"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_selection_attempt"),
    )
    sa.Index(
        "ix_selection_preview_work",
        selection_preview.c.status,
        selection_preview.c.next_attempt_at,
        selection_preview.c.lease_expires_at,
    )
    sa.Index(
        "ix_selection_preview_expiry",
        selection_preview.c.status,
        selection_preview.c.expires_at,
    )

    policy_version = sa.Table(
        "policy_version",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("document_ref", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default=EMPTY_ARRAY),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "id", name="uq_policy_version_tenant_id"),
        sa.UniqueConstraint("tenant_id", "version", name="uq_policy_version_number"),
        sa.UniqueConstraint("tenant_id", "digest", name="uq_policy_version_digest"),
    )
    sa.Index(
        "uq_policy_version_active",
        policy_version.c.tenant_id,
        unique=True,
        postgresql_where=sa.text("active"),
    )

    report_schedule = sa.Table(
        "report_schedule",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("local_time", sa.Time(), nullable=False),
        sa.Column("selection_mode", sa.String(32), nullable=False),
        sa.Column("selector", postgresql.JSONB()),
        sa.Column("fixed_skus_ref", sa.Text()),
        sa.Column("fixed_skus_hash", sa.String(64)),
        sa.Column("policy_mode", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.Integer()),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("next_fire_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.UniqueConstraint("tenant_id", "id", name="uq_report_schedule_tenant_id"),
    )
    sa.Index(
        "ix_report_schedule_due",
        report_schedule.c.active,
        report_schedule.c.next_fire_at_utc,
    )

    report_run = sa.Table(
        "report_run",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("client_request_id", sa.String(200), nullable=False),
        sa.Column("selection_preview_id", postgresql.UUID(as_uuid=True)),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True)),
        sa.Column("scheduled_fire_at_utc", sa.DateTime(timezone=True)),
        sa.Column("policy_mode", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.Integer()),
        sa.Column("policy_snapshot_ref", sa.Text()),
        sa.Column("data_cutoff", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("sku_count", sa.Integer(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("complete_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("risk_flags", postgresql.JSONB(), nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(256)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "id", name="uq_report_run_tenant_id"),
        sa.UniqueConstraint("tenant_id", "client_request_id", name="uq_report_run_request"),
        sa.UniqueConstraint(
            "tenant_id",
            "schedule_id",
            "scheduled_fire_at_utc",
            name="uq_report_run_scheduled_fire",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "selection_preview_id"],
            [f"{SCHEMA}.selection_preview.tenant_id", f"{SCHEMA}.selection_preview.id"],
            name="fk_report_run_selection",
        ),
        sa.CheckConstraint("sku_count BETWEEN 0 AND 10000", name="ck_report_run_sku_count"),
        sa.CheckConstraint("batch_count BETWEEN 0 AND 50", name="ck_report_run_batch_count"),
        sa.CheckConstraint("complete_count BETWEEN 0 AND sku_count", name="ck_report_run_complete"),
        sa.CheckConstraint("blocked_count BETWEEN 0 AND sku_count", name="ck_report_run_blocked"),
        sa.CheckConstraint("failed_count BETWEEN 0 AND sku_count", name="ck_report_run_failed"),
    )
    sa.Index(
        "ix_report_run_status",
        report_run.c.tenant_id,
        report_run.c.status,
        report_run.c.created_at,
    )

    report_batch = sa.Table(
        "report_batch",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("report_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_no", sa.Integer(), nullable=False),
        sa.Column("item_offset", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("complete_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(256)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("runtime_execution_id", postgresql.UUID(as_uuid=True)),
        sa.Column("runtime_sequence", sa.BigInteger(), nullable=False),
        sa.Column("result_ref", sa.Text()),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("safe_error", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.ForeignKeyConstraint(
            ["tenant_id", "report_run_id"],
            [f"{SCHEMA}.report_run.tenant_id", f"{SCHEMA}.report_run.id"],
            name="fk_report_batch_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_report_batch_tenant_id"),
        sa.UniqueConstraint("tenant_id", "report_run_id", "batch_no", name="uq_report_batch_no"),
        sa.UniqueConstraint("tenant_id", "runtime_execution_id", name="uq_report_batch_execution"),
        sa.CheckConstraint("batch_no BETWEEN 1 AND 50", name="ck_report_batch_no"),
        sa.CheckConstraint("item_count BETWEEN 1 AND 200", name="ck_report_batch_item_count"),
        sa.CheckConstraint("item_offset >= 0", name="ck_report_batch_offset"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_report_batch_attempt"),
    )
    sa.Index(
        "ix_report_batch_dispatch",
        report_batch.c.status,
        report_batch.c.next_attempt_at,
        report_batch.c.lease_expires_at,
    )

    report_outbox = sa.Table(
        "report_outbox",
        metadata,
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("report_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(256)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            [f"{SCHEMA}.report_batch.tenant_id", f"{SCHEMA}.report_batch.id"],
            name="fk_report_outbox_batch",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "event_id", name="uq_report_outbox_event"),
    )
    sa.Index(
        "ix_report_outbox_publish",
        report_outbox.c.status,
        report_outbox.c.next_attempt_at,
        report_outbox.c.lease_expires_at,
    )

    report_inbox = sa.Table(
        "report_inbox",
        metadata,
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW
        ),
        sa.UniqueConstraint("tenant_id", "event_id", name="uq_report_inbox_event"),
    )

    report_activity = sa.Table(
        "report_activity",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("stream_sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("report_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True)),
        sa.Column("event_key", sa.String(256), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("phase", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("safe_message", sa.String(1024), nullable=False),
        sa.Column("progress_current", sa.Integer()),
        sa.Column("progress_total", sa.Integer()),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("critical", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.ForeignKeyConstraint(
            ["tenant_id", "report_run_id"],
            [f"{SCHEMA}.report_run.tenant_id", f"{SCHEMA}.report_run.id"],
            name="fk_report_activity_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "report_run_id", "event_key", name="uq_report_activity"),
    )
    sa.Index(
        "ix_report_activity_replay",
        report_activity.c.tenant_id,
        report_activity.c.report_run_id,
        report_activity.c.stream_sequence,
    )

    return (
        selection_preview,
        policy_version,
        report_schedule,
        report_run,
        report_batch,
        report_outbox,
        report_inbox,
        report_activity,
    )


def upgrade() -> None:
    bind = op.get_bind()
    for table in _level2_tables():
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Production rollback is additive and non-destructive by policy.
    pass
