"""arc_templates.language

Authored-prose language tag for shipped / user-authored arc templates
(SHIPPED_CONTENT_LOCALIZATION_PLAN, Phase 1). Bundled packs ship
``zh-TW``; the column is metadata only — the picker surfaces it as a
source-language badge and the materialise path reads it to decide
whether to LLM-translate the arc into the operator's primary language.
It is never used to filter the catalogue.

Revision ID: f1a2c3d40205
Revises: r4c6f8t00203
Create Date: 2026-07-05 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f1a2c3d40205"
down_revision: Union[str, None] = "r4c6f8t00203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default carries existing rows (all bundled zh-TW packs +
    # user-authored rows) through; the application is authoritative
    # post-migration so we drop the default afterwards, matching how
    # story_arcs.tone and the other columns on this table behave.
    op.add_column(
        "arc_templates",
        sa.Column(
            "language",
            sa.String(length=16),
            nullable=False,
            server_default="zh-TW",
        ),
    )
    op.alter_column("arc_templates", "language", server_default=None)


def downgrade() -> None:
    op.drop_column("arc_templates", "language")
