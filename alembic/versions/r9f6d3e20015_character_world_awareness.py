"""add characters.world_awareness_enabled + world_topics

Opt-in flag and per-character topic filter for the world-event prompt
injection. Both default to off / empty so existing characters continue
to get the exact same prompt.

Revision ID: r9f6d3e20015
Revises: q8e5c2d10014
Create Date: 2026-04-19 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "r9f6d3e20015"
down_revision: Union[str, None] = "q8e5c2d10014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "world_awareness_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "world_topics",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "world_topics")
    op.drop_column("characters", "world_awareness_enabled")
