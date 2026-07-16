"""emotion events

Revision ID: ch3m5n00058
Revises: cg2l4m90057
Create Date: 2026-05-20 12:00:00.000000

Add ``emotion_events`` — the per-cause emotion event log behind the
EmotionAggregator. Existing ``CharacterState`` flat columns continue
to act as the baseline; the aggregator integrates events over a
24h window onto that baseline to produce the prompt-side snapshot.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ch3m5n00058"
down_revision: Union[str, None] = "cg2l4m90057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "emotion_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("cause_ref_kind", sa.String(length=32), nullable=False),
        sa.Column("cause_ref_id", sa.String(length=64), nullable=True),
        sa.Column("valence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("arousal", sa.Float(), nullable=False, server_default="0"),
        sa.Column("intensity", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("affection_delta", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fatigue_delta", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trust_delta", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("energy_delta", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("emotion_label", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_quote", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "decay_half_life_minutes",
            sa.Integer(),
            nullable=False,
            server_default="240",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_emotion_events_char_op_created",
        "emotion_events",
        ["character_id", "operator_id", "created_at"],
    )
    op.create_index(
        "ix_emotion_events_cause",
        "emotion_events",
        ["cause_ref_kind", "cause_ref_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_emotion_events_cause", table_name="emotion_events")
    op.drop_index("ix_emotion_events_char_op_created", table_name="emotion_events")
    op.drop_table("emotion_events")
