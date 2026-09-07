"""Add durable idempotency records for schedule create and update."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_schedule_idempotency"
down_revision = "0004_runtime_evidence_refs"
branch_labels = None
depends_on = None

SCHEMA = "supply_chain_bff"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("schedule_idempotency", schema=SCHEMA):
        return
    op.create_table(
        "schedule_idempotency",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "schedule_id"],
            [f"{SCHEMA}.report_schedule.tenant_id", f"{SCHEMA}.report_schedule.id"],
            name="fk_schedule_idempotency_schedule",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_schedule_idempotency_key",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("schedule_idempotency", schema=SCHEMA)
