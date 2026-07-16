"""add feed_reactions table

Likes on character feed-wall posts (Phase A1). Single-row-per-(post,
liker) model — Phase A1 only handles likes so there is no kind column;
adding more reaction shapes later means a new column or a new table,
not retro-mutating this one.

Cascades from ``feed_posts``: deleting a post must remove its likes so
the denormalised ``likes_count`` on the post row can never drift
positive after the parent disappears. ``liker_id`` is free-form text;
single-user mode stamps ``"local"`` today, but messaging-bot likes
(Telegram / LINE) can drop their own identity in without a migration.

Revision ID: ae4i6d20029
Revises: ad3h5c10028
Create Date: 2026-04-29 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "ae4i6d20029"
down_revision: Union[str, None] = "ad3h5c10028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feed_reactions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "post_id",
            sa.String(length=36),
            sa.ForeignKey("feed_posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("liker_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_feed_reactions_post_id",
        "feed_reactions",
        ["post_id"],
    )
    op.create_index(
        "ix_feed_reactions_liker_id",
        "feed_reactions",
        ["liker_id"],
    )
    op.create_index(
        "ix_feed_reactions_created_at",
        "feed_reactions",
        ["created_at"],
    )
    op.create_unique_constraint(
        "uq_feed_reactions_post_liker",
        "feed_reactions",
        ["post_id", "liker_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_feed_reactions_post_liker",
        "feed_reactions",
        type_="unique",
    )
    op.drop_index(
        "ix_feed_reactions_created_at", table_name="feed_reactions",
    )
    op.drop_index(
        "ix_feed_reactions_liker_id", table_name="feed_reactions",
    )
    op.drop_index(
        "ix_feed_reactions_post_id", table_name="feed_reactions",
    )
    op.drop_table("feed_reactions")
