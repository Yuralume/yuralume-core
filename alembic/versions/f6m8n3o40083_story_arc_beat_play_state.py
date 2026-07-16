"""story arc beat play-attempt state

Revision ID: f6m8n3o40083
Revises: e5l7m2n30082
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f6m8n3o40083"
down_revision = "e5l7m2n30082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "story_arc_beats",
        sa.Column(
            "play_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column("last_play_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column("last_play_attempt_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column("last_play_attempt_result", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column("last_play_push_intensity", sa.String(length=32), nullable=True),
    )
    op.alter_column("story_arc_beats", "play_attempt_count", server_default=None)


def downgrade() -> None:
    op.drop_column("story_arc_beats", "last_play_push_intensity")
    op.drop_column("story_arc_beats", "last_play_attempt_result")
    op.drop_column("story_arc_beats", "last_play_attempt_source")
    op.drop_column("story_arc_beats", "last_play_attempt_at")
    op.drop_column("story_arc_beats", "play_attempt_count")
