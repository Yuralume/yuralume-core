"""character.arc_template_id

Phase 2 of ``docs/SCENE_BEAT_PLAN.md`` — characters can opt into a
hand-written arc template (YAML in ``data/arc_templates/``); the
``StoryArcService`` materialises the template instead of LLM-planning a
fresh arc when this field is set. NULL = legacy LLM-only behaviour.

Not a foreign key — templates live in YAML files, not DB rows.

Revision ID: ab1f3a80026
Revises: aa9e2f70025
Create Date: 2026-04-26 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "ab1f3a80026"
down_revision: Union[str, None] = "aa9e2f70025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("arc_template_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("characters", "arc_template_id")
