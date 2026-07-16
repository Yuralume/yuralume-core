"""add story_seeds, story_events, characters.world_frame

Story gacha — per-character life events so non-modern personas have
novelty without leaning on RSS world events.

Revision ID: s1a7e4f00016
Revises: r9f6d3e20015
Create Date: 2026-04-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "s1a7e4f00016"
down_revision: Union[str, None] = "r9f6d3e20015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "world_frame",
            sa.String(length=40),
            nullable=False,
            server_default="modern",
        ),
    )

    op.create_table(
        "story_seeds",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("seed_text", sa.Text(), nullable=False),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "world_frames",
            sa.Text(),
            nullable=False,
            server_default='["any"]',
        ),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column(
            "cooldown_days", sa.Integer(), nullable=False, server_default="7",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
        ),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "external_id", sa.String(length=120), nullable=True, unique=True,
        ),
        sa.Column("pack_id", sa.String(length=80), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "story_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("date", sa.String(length=10), nullable=False, index=True),
        sa.Column(
            "seed_id",
            sa.String(length=36),
            sa.ForeignKey("story_seeds.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("emotional_tone", sa.String(length=60), nullable=True),
        sa.Column(
            "memorialized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "character_id", "date", "seed_id",
            name="uq_story_events_character_date_seed",
        ),
    )


def downgrade() -> None:
    op.drop_table("story_events")
    op.drop_table("story_seeds")
    op.drop_column("characters", "world_frame")
