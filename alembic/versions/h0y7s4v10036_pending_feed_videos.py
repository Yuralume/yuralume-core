"""pending_feed_videos — deferred LumeGram video posts (CV4).

The asynchronous I2V pipeline publishes a post minutes after the tick
that composed it, on a different process. This table is what survives in
between: the composed draft, the already-rendered first frame, the job
ticket and its deadline. Without it a restart would strand the post
forever — the clip would render and nobody would ever look for it.

Additive, no data change, empty on upgrade — safe to roll back (the
pipeline only writes rows on deployments whose video provider takes
jobs, which is the hosted cloud adapter alone).

Revision ID: h0y7s4v10036
Revises: g9x6r3u10035
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "h0y7s4v10036"
down_revision = "g9x6r3u10035"
branch_labels = None
depends_on = None


_TABLE = "pending_feed_videos"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "operator_id", sa.String(length=64), nullable=False, server_default="",
        ),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column(
            "post_kind", sa.String(length=32), nullable=False, server_default="daily",
        ),
        sa.Column(
            "source_kind",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("source_ref_id", sa.String(length=64), nullable=True),
        sa.Column("first_frame_url", sa.Text(), nullable=False),
        sa.Column(
            "first_frame_key", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column("image_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("video_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "timeout_seconds", sa.Integer(), nullable=False, server_default="900",
        ),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("post_id", sa.String(length=36), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        f"ix_{_TABLE}_character_id", _TABLE, ["character_id"],
    )
    op.create_index(f"ix_{_TABLE}_job_id", _TABLE, ["job_id"])
    op.create_index(f"ix_{_TABLE}_status", _TABLE, ["status"])
    # The sweep's only query: "pending rows whose next observation is due".
    op.create_index(
        f"ix_{_TABLE}_status_next_poll", _TABLE, ["status", "next_poll_at"],
    )


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_status_next_poll", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_status", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_job_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_character_id", table_name=_TABLE)
    op.drop_table(_TABLE)
