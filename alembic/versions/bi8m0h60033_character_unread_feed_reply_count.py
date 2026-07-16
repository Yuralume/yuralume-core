"""add characters.unread_feed_reply_count

Phase B follow-up: surface a red-dot badge on the StagePage LumeGram
launcher when a scheduler-tick reply landed since the last time the
user opened the overlay. Mirrors the existing
``unread_proactive_count`` field — same shape, same lifecycle (write
on the producer side, zero on the seen-side endpoint).

Existing rows backfill to 0 via ``server_default="0"`` so upgrading
deployments don't temporarily render a phantom badge before the next
scheduler tick.

Revision ID: bi8m0h60033
Revises: bh7l9g50032
Create Date: 2026-04-30 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bi8m0h60033"
down_revision: Union[str, None] = "bh7l9g50032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "unread_feed_reply_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "unread_feed_reply_count")
