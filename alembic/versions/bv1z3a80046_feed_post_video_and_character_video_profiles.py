"""feed_post.video + character.feature_video_profiles

Two new columns landing together so the video-on-feed feature has all
the schema in one revision:

  * ``feed_posts.video_url`` + ``feed_posts.video_prompt`` (both
    nullable Text) — populated when the LLM composer picks
    ``media_kind=video`` and Wan2.2 generation succeeds. Frontend
    prefers ``video_url`` over ``image_url`` when both are present.

  * ``characters.feature_video_profiles_json`` (Text NOT NULL DEFAULT
    '[]') — per-character video-profile overrides, mirror of the
    existing ``feature_image_profiles_json`` column.

NULL ``video_url`` is the "image-only / text-only post" baseline so
every existing row stays valid without a backfill. Same for the
character column — empty list is the legal "no overrides" state.

Revision ID: bv1z3a80046
Revises: bu0y2z70045
Create Date: 2026-05-12 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bv1z3a80046"
down_revision: Union[str, None] = "bu0y2z70045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "feed_posts",
        sa.Column("video_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "feed_posts",
        sa.Column("video_prompt", sa.Text(), nullable=True),
    )
    op.add_column(
        "characters",
        sa.Column(
            "feature_video_profiles_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "feature_video_profiles_json")
    op.drop_column("feed_posts", "video_prompt")
    op.drop_column("feed_posts", "video_url")
