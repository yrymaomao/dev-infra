"""Add revocable OpenClaw run bindings for the Tool Gateway."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_openclaw_run_binding"
down_revision = "0006_report_v2_contract"
branch_labels = None
depends_on = None

SCHEMA = "supply_chain_bff"


def upgrade() -> None:
    op.create_table(
        "openclaw_run_binding",
        sa.Column("run_id", sa.String(512), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("principal_id", sa.String(256), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("session_key", sa.String(256), nullable=False),
        sa.Column("selector", sa.String(256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("tenant_id", "session_key", name="uq_openclaw_run_session"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_openclaw_run_active",
        "openclaw_run_binding",
        ["tenant_id", "active", "expires_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_openclaw_run_active", table_name="openclaw_run_binding", schema=SCHEMA)
    op.drop_table("openclaw_run_binding", schema=SCHEMA)
