"""story_seeds.language

Provenance language tag for shipped / imported story seeds
(SHIPPED_CONTENT_LOCALIZATION_PLAN, Phase 4). Bundled packs ship
``zh-TW``; when ``cli.import_story_seeds --translate`` localizes a
seed's ``seed_text`` into the operator's primary language it stamps the
tag here. Metadata only — the seed management UI badges the source; it
is never used to filter seeds (the runtime expander already localizes
generated output via the operator language hint, so filtering would
only empty an en/ja operator's pool).

Revision ID: f2b3d4e50206
Revises: f1a2c3d40205
Create Date: 2026-07-05 00:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f2b3d4e50206"
down_revision: Union[str, None] = "f1a2c3d40205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "story_seeds",
        sa.Column(
            "language",
            sa.String(length=16),
            nullable=False,
            server_default="zh-TW",
        ),
    )
    op.alter_column("story_seeds", "language", server_default=None)


def downgrade() -> None:
    op.drop_column("story_seeds", "language")
