"""self_reflections table — dream-time inner narrative

Revision ID: cm8r0s50063
Revises: cl7q9r40062
Create Date: 2026-05-21 05:00:00.000000

HUMANIZATION_ROADMAP §3.2 — the dream pass periodically writes a short
first-person narrative ("我這週過得怎麼樣") per (character, operator,
period). The chat / proactive prompt builder injects this as a fact-
layer block so the character's voice carries a sense of continuity.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cm8r0s50063"
down_revision: Union[str, None] = "cl7q9r40062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "self_reflections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("dominant_themes", sa.Text(), nullable=False, server_default=""),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("evidence_quotes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "character_id", "operator_id", "period",
            name="uq_self_reflections_per_pair_period",
        ),
    )
    op.create_index(
        "ix_self_reflections_character_id",
        "self_reflections",
        ["character_id"],
    )
    op.create_index(
        "ix_self_reflections_created_at",
        "self_reflections",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_self_reflections_created_at", table_name="self_reflections",
    )
    op.drop_index(
        "ix_self_reflections_character_id", table_name="self_reflections",
    )
    op.drop_table("self_reflections")
