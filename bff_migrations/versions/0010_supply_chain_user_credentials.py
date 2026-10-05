"""Pin user ownership and opaque credential references on new Level 2 work."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_supply_chain_user_credentials"
down_revision = "0009_conversation_profile_index"
branch_labels = None
depends_on = None

SCHEMA = "supply_chain_bff"
TABLES = ("selection_preview", "report_run", "report_schedule")


def upgrade() -> None:
    # Alembic creates version_num as VARCHAR(32); this revision identifier is
    # longer, so widen the column before Alembic records the new head.
    op.alter_column(
        "alembic_version",
        "version_num",
        schema=SCHEMA,
        existing_type=sa.String(32),
        type_=sa.String(64),
    )
    for table in TABLES:
        op.add_column(table, sa.Column("owner_uid", sa.String(256)), schema=SCHEMA)
        op.add_column(table, sa.Column("credential_ref", sa.String(256)), schema=SCHEMA)
        op.create_index(
            f"ix_{table}_owner",
            table,
            ["tenant_id", "owner_uid"],
            schema=SCHEMA,
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_owner", table_name=table, schema=SCHEMA)
        op.drop_column(table, "credential_ref", schema=SCHEMA)
        op.drop_column(table, "owner_uid", schema=SCHEMA)
