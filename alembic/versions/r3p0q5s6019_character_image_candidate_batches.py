"""character_image_candidate_batches — cross-replica album gacha state.

The album candidate flow is two requests: ``generate_candidates`` writes N
throwaway objects under ``characters/{id}/candidates/{batch_id}/`` and
``commit_candidates`` decides which of them to keep. The batch's object keys
were kept in a per-process dict, which is wrong under the hosted topology
(``core-router`` load-balancing 2× api): a commit served by the other replica
saw an empty batch and silently dropped every image the player picked, and the
uncommitted objects leaked because object storage has no list-by-prefix call.

One row per character — a second generate supersedes the first, matching the UI
where only the latest batch is on screen — deleted once the batch is committed
or discarded, so the table stays tiny. No backfill: any batch pending at deploy
time lived only in the old process memory and is already unrecoverable.

Revision ID: r3p0q5s6019
Revises: q2o9p4r5018
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "r3p0q5s6019"
down_revision = "q2o9p4r5018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_image_candidate_batches",
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("object_keys_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("character_id"),
    )


def downgrade() -> None:
    op.drop_table("character_image_candidate_batches")
