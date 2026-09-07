"""Record the public report contract generation for historical v1 reads."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_report_v2_contract"
down_revision = "0005_schedule_idempotency"
branch_labels = None
depends_on = None

SCHEMA = "supply_chain_bff"


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("report_run", schema=SCHEMA)
    }
    if "report_schema_version" in columns:
        return
    op.add_column(
        "report_run",
        sa.Column(
            "report_schema_version",
            sa.String(64),
            nullable=False,
            server_default="supply-chain.report.v1",
        ),
        schema=SCHEMA,
    )
    op.alter_column(
        "report_run",
        "report_schema_version",
        server_default="supply-chain.report.v2",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("report_run", "report_schema_version", schema=SCHEMA)
