"""story_arcs.tone

Per-arc tonal register (daily / dramatic / mature / dark / lighthearted /
custom). Set at materialise time from ArcTemplate.tone (Phase 2 of
SCENE_BEAT_PLAN); LLM-planned arcs default to 'daily'. Drives the
expander prompt switch so the same scene structure can read as gentle
slice-of-life or grim military drama.

Revision ID: ac2g4b90027
Revises: ab1f3a80026
Create Date: 2026-04-26 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "ac2g4b90027"
down_revision: Union[str, None] = "ab1f3a80026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default carries existing rows through; the application
    # is the source of truth post-migration so we drop the default
    # afterwards (matches how other columns on this table behave).
    op.add_column(
        "story_arcs",
        sa.Column(
            "tone",
            sa.String(length=32),
            nullable=False,
            server_default="daily",
        ),
    )
    op.alter_column("story_arcs", "tone", server_default=None)


def downgrade() -> None:
    op.drop_column("story_arcs", "tone")
