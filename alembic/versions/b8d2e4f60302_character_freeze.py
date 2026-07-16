"""characters.frozen / frozen_at / created_at (CHARACTER_FREEZE_PLAN)

Site-level cost-control freeze. ``frozen`` halts all background
scheduler activity for a character while preserving its persisted
state; foreground chat auto-unfreezes it. ``frozen_at`` records the
freeze instant for admin display. ``created_at`` is a server-managed
row-creation timestamp used by the auto-freeze idle-sweep reaper as the
anchor for characters the user has never chatted with (existing rows
are backfilled to the migration apply-time).

Revision ID: b8d2e4f60302
Revises: f2b3d4e50206
Create Date: 2026-07-08 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b8d2e4f60302"
down_revision: Union[str, None] = "f2b3d4e50206"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "frozen",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "frozen_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "created_at")
    op.drop_column("characters", "frozen_at")
    op.drop_column("characters", "frozen")
