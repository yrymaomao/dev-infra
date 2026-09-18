"""Index conversations by reception profile and tenant (expand-only)."""

from alembic import op

revision = "0009_conversation_profile_index"
down_revision = "0008_conversation_reception"
branch_labels = None
depends_on = None
S = "supply_chain_bff"
INDEX = "ix_openclaw_conversation_profile_tenant"


def upgrade() -> None:
    # Two reception profiles (Supply Chain, CRM) share the conversation tables
    # and are told apart by ``profile_version``; every owner lookup, worker
    # claim and credential exchange filters on it together with the tenant.
    op.create_index(
        INDEX,
        "openclaw_conversation",
        ["profile_version", "tenant_id"],
        schema=S,
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="openclaw_conversation", schema=S)
