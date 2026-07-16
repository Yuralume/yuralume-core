"""add feed_comments table

User comments on character feed-wall posts (Phase A2). Comments are not
idempotent — every submission creates a row. ``author_id`` is free-form
text; single-user mode stamps ``"local"`` today, but messaging bots can
drop their own identity in without a migration.

Cascades from ``feed_posts``: deleting a post removes its comments so
the denormalised ``comments_count`` can never drift positive after the
parent disappears.

Revision ID: af5j7e30030
Revises: ae4i6d20029
Create Date: 2026-04-29 16:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "af5j7e30030"
down_revision: Union[str, None] = "ae4i6d20029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feed_comments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "post_id",
            sa.String(length=36),
            sa.ForeignKey("feed_posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_id", sa.String(length=64), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_feed_comments_post_id",
        "feed_comments",
        ["post_id"],
    )
    op.create_index(
        "ix_feed_comments_author_id",
        "feed_comments",
        ["author_id"],
    )
    op.create_index(
        "ix_feed_comments_created_at",
        "feed_comments",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feed_comments_created_at", table_name="feed_comments",
    )
    op.drop_index(
        "ix_feed_comments_author_id", table_name="feed_comments",
    )
    op.drop_index(
        "ix_feed_comments_post_id", table_name="feed_comments",
    )
    op.drop_table("feed_comments")
