"""deferred_intents table — proactive motive half-life

Revision ID: ck6p8q30061
Revises: cj5o7p20060
Create Date: 2026-05-21 03:30:00.000000

HUMANIZATION_ROADMAP §3.4 — when the proactive ``intention_judge`` blocks
a slot, the inner motive is parked here with a TTL (default 24h) instead
of being silently discarded. The next judge pass surfaces still-active
motives as a fact-layer prompt block so the LLM can re-evaluate timing.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ck6p8q30061"
down_revision: Union[str, None] = "cj5o7p20060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deferred_intents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column(
            "trigger",
            sa.String(length=48),
            nullable=False,
            server_default="tick",
        ),
        sa.Column("inner_motive", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "conversation_purpose", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column("expected_reply", sa.Text(), nullable=False, server_default=""),
        sa.Column("risk", sa.Text(), nullable=False, server_default=""),
        sa.Column("best_timing", sa.Text(), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_deferred_intents_character_id",
        "deferred_intents",
        ["character_id"],
    )
    op.create_index(
        "ix_deferred_intents_status",
        "deferred_intents",
        ["status"],
    )
    op.create_index(
        "ix_deferred_intents_created_at",
        "deferred_intents",
        ["created_at"],
    )
    op.create_index(
        "ix_deferred_intents_expires_at",
        "deferred_intents",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deferred_intents_expires_at", table_name="deferred_intents",
    )
    op.drop_index(
        "ix_deferred_intents_created_at", table_name="deferred_intents",
    )
    op.drop_index(
        "ix_deferred_intents_status", table_name="deferred_intents",
    )
    op.drop_index(
        "ix_deferred_intents_character_id", table_name="deferred_intents",
    )
    op.drop_table("deferred_intents")
