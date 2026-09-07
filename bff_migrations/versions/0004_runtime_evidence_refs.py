"""Persist Runtime-governed EvidenceRef identities for Level 2 reports."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_runtime_evidence_refs"
down_revision = "0003_batch_selection_payload"
branch_labels = None
depends_on = None

SCHEMA = "supply_chain_bff"


def upgrade() -> None:
    report_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("report_run", schema=SCHEMA)
    }
    if "policy_snapshot_hash" not in report_columns:
        op.add_column(
            "report_run",
            sa.Column("policy_snapshot_hash", sa.String(64), nullable=True),
            schema=SCHEMA,
        )
    if "policy_evidence_id" not in report_columns:
        op.add_column(
            "report_run",
            sa.Column(
                "policy_evidence_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            schema=SCHEMA,
        )

    batch_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("report_batch", schema=SCHEMA)
    }
    if "selection_evidence_id" not in batch_columns:
        op.add_column(
            "report_batch",
            sa.Column(
                "selection_evidence_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            schema=SCHEMA,
        )


def downgrade() -> None:
    op.drop_column("report_batch", "selection_evidence_id", schema=SCHEMA)
    op.drop_column("report_run", "policy_evidence_id", schema=SCHEMA)
    op.drop_column("report_run", "policy_snapshot_hash", schema=SCHEMA)
