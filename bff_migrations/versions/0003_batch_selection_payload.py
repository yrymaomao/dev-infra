"""Bind each report batch to its own bounded restricted selection artifact."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_batch_selection_payload"
down_revision = "0002_supply_chain_level2"
branch_labels = None
depends_on = None

SCHEMA = "supply_chain_bff"


def upgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("report_batch", schema=SCHEMA)
    }
    if "selection_payload_ref" not in existing:
        op.add_column(
            "report_batch",
            sa.Column("selection_payload_ref", sa.Text(), nullable=True),
            schema=SCHEMA,
        )
    if "selection_payload_hash" not in existing:
        op.add_column(
            "report_batch",
            sa.Column("selection_payload_hash", sa.String(64), nullable=True),
            schema=SCHEMA,
        )


def downgrade() -> None:
    op.drop_column("report_batch", "selection_payload_hash", schema=SCHEMA)
    op.drop_column("report_batch", "selection_payload_ref", schema=SCHEMA)
