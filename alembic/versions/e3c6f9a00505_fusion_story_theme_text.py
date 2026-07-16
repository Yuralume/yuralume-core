"""Allow narrative-length fusion story themes.

The fusion planner treats ``theme`` as player-visible natural language and can
legitimately return a sentence. The original VARCHAR(64) columns therefore
made a successful outline fail during persistence.

Revision ID: e3c6f9a00505
Revises: d2b5e8f90404
Create Date: 2026-07-13 06:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e3c6f9a00505"
down_revision: str | Sequence[str] | None = "d2b5e8f90404"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "fusion_stories",
        "theme",
        existing_type=sa.String(length=64),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "fusion_story_versions",
        "theme",
        existing_type=sa.String(length=64),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "fusion_story_versions",
        "theme",
        existing_type=sa.Text(),
        type_=sa.String(length=64),
        existing_nullable=False,
        postgresql_using="left(theme, 64)",
    )
    op.alter_column(
        "fusion_stories",
        "theme",
        existing_type=sa.Text(),
        type_=sa.String(length=64),
        existing_nullable=False,
        postgresql_using="left(theme, 64)",
    )
