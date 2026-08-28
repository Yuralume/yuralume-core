"""feed_posts.viewed_at — KB11 of PLAYER_KNOWLEDGE_BOUNDARY_PLAN

The disclosure ledger (Phase 2, KB8) needs to know whether the player
actually looked at a feed post before it can flip that post's source
``private`` memory to ``disclosed`` — "she posted it" is not the same
fact as "the player read it". This column is the read-side foundation
KB11 asks for; nothing downstream reads it yet.

Single-user world, one reader per post, so one nullable timestamp on
the post row is enough — no separate views table. ``NULL`` means
"never viewed", which is also the correct value for every existing
row: zero backfill, per D7 of that plan (a wrong guess in either
direction is worse than "unknown"; existing posts predate the reader
even being tracked).

Revision ID: x4l0f7i10049
Revises: w3k9e6h10048
Create Date: 2026-08-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "x4l0f7i10049"
down_revision: Union[str, None] = "w3k9e6h10048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "feed_posts",
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("feed_posts", "viewed_at")
