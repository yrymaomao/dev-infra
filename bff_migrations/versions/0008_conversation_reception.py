"""Add isolated conversation, turn and event storage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0008_conversation_reception"
down_revision = "0007_openclaw_run_binding"
branch_labels = None
depends_on = None
S = "supply_chain_bff"


def upgrade() -> None:
    op.create_table(
        "openclaw_conversation",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(256), nullable=False),
        sa.Column("principal_id", sa.String(256), nullable=False),
        sa.Column("profile_version", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=S,
    )
    op.create_table(
        "openclaw_conversation_turn",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.openclaw_conversation.id"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("prompt_ref", sa.Text(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("conversation_id", "request_id", name="uq_chat_turn_request"),
        schema=S,
    )
    op.create_index("ix_chat_turn_state", "openclaw_conversation_turn", ["state"], schema=S)
    op.create_table(
        "openclaw_conversation_event",
        sa.Column(
            "turn_id",
            UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.openclaw_conversation_turn.id"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload_ref", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        schema=S,
    )
    op.add_column("openclaw_run_binding", sa.Column("turn_id", UUID(as_uuid=True)), schema=S)
    op.create_foreign_key(
        "fk_openclaw_run_turn",
        "openclaw_run_binding",
        "openclaw_conversation_turn",
        ["turn_id"],
        ["id"],
        source_schema=S,
        referent_schema=S,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Conversation history is retained; use an explicitly reviewed rollback migration"
    )
