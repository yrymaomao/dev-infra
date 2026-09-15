"""Create the additive Supply Chain BFF batch schema."""

from __future__ import annotations

from alembic import op

from ebiz_deployment.supply_chain_bff.models import SCHEMA, Base

revision = "0001_supply_chain_bff"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    # Only the three tables owned by this revision. Importing later ORM models
    # must not create future tables ahead of their authoritative migrations.
    tables = [Base.metadata.tables[f"{SCHEMA}.{name}"] for name in (
        "agent_execution_batch", "agent_execution_batch_item", "agent_execution_batch_activity",
    )]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=True)


def downgrade() -> None:
    # Emergency rollback keeps additive data by policy. Destructive cleanup is
    # an explicit operator action, not part of a release rollback.
    pass
