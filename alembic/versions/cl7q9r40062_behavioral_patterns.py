"""behavioral_patterns table — recurring activity / phrase / time preference

Revision ID: cl7q9r40062
Revises: ck6p8q30061
Create Date: 2026-05-21 04:30:00.000000

HUMANIZATION_ROADMAP §3.3 — store statistically recurring shapes of the
character's life (schedule recurrences, verbal habits, time preference).
Dream pass upserts on ``(character_id, kind, description)`` so reruns
bump ``observed_count`` instead of bloating the table.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cl7q9r40062"
down_revision: Union[str, None] = "ck6p8q30061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "behavioral_patterns",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("observed_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("salience", sa.Float(), nullable=False, server_default="0.5"),
        sa.UniqueConstraint(
            "character_id", "kind", "description",
            name="uq_behavioral_patterns_per_character_kind_desc",
        ),
    )
    op.create_index(
        "ix_behavioral_patterns_character_id",
        "behavioral_patterns",
        ["character_id"],
    )
    op.create_index(
        "ix_behavioral_patterns_kind",
        "behavioral_patterns",
        ["kind"],
    )
    op.create_index(
        "ix_behavioral_patterns_last_observed_at",
        "behavioral_patterns",
        ["last_observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_behavioral_patterns_last_observed_at",
        table_name="behavioral_patterns",
    )
    op.drop_index(
        "ix_behavioral_patterns_kind",
        table_name="behavioral_patterns",
    )
    op.drop_index(
        "ix_behavioral_patterns_character_id",
        table_name="behavioral_patterns",
    )
    op.drop_table("behavioral_patterns")
