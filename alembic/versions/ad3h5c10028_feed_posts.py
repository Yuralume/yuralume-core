"""add feed_posts table + characters.feed_daily_limit

Character feed wall (動態牆) — Instagram-style autonomous posts the
character publishes during ProactiveScheduler ticks. Each post pairs a
short narrative with an optional generated image and a provenance
pointer back to whatever domain object inspired it (a schedule
activity, story beat, memory, world event, or derived signal like
"user has been silent").

The ``UNIQUE(character_id, source_kind, source_ref_id)`` constraint
drives composer-time dedup so the same beat can't seed two posts even
if a tick fires twice. ``source_ref_id`` is part of the constraint
even when NULL because Postgres treats NULLs as distinct in unique
indices — that's intentional: silence-derived posts (no ref id) should
be allowed to repeat at most once per cooldown window, which the
service layer enforces separately.

``feed_daily_limit`` lives on ``characters`` so the operator can tune
posting cadence per character via the UI; default 3 mirrors the
existing ``proactive_daily_limit`` convention.

Revision ID: ad3h5c10028
Revises: aa01b5g70025
Create Date: 2026-04-29 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "ad3h5c10028"
down_revision: Union[str, None] = "aa01b5g70025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "feed_daily_limit",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
    )
    op.alter_column("characters", "feed_daily_limit", server_default=None)

    op.create_table(
        "feed_posts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="daily",
        ),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column(
            "source_kind",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("source_ref_id", sa.String(length=64), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("image_prompt", sa.Text(), nullable=True),
        sa.Column(
            "likes_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "comments_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reactions_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_feed_posts_character_id_created_at",
        "feed_posts",
        ["character_id", "created_at"],
    )
    op.create_index(
        "ix_feed_posts_character_id_source",
        "feed_posts",
        ["character_id", "source_kind", "source_ref_id"],
    )
    op.create_unique_constraint(
        "uq_feed_posts_character_source",
        "feed_posts",
        ["character_id", "source_kind", "source_ref_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_feed_posts_character_source",
        "feed_posts",
        type_="unique",
    )
    op.drop_index(
        "ix_feed_posts_character_id_source",
        table_name="feed_posts",
    )
    op.drop_index(
        "ix_feed_posts_character_id_created_at",
        table_name="feed_posts",
    )
    op.drop_table("feed_posts")
    op.drop_column("characters", "feed_daily_limit")
