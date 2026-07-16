"""characters.frozen_reason (CHARACTER_FREEZE_PLAN — subscription lapse layering)

Adds a nullable ``frozen_reason`` provenance column so a freeze can be
thawed according to how it was applied:
  - ``idle``               → auto-sweep reaper; foreground chat auto-unfreezes.
  - ``manual``             → admin console; sticky (chat does not undo it).
  - ``subscription_lapse`` → Cloud tenant tier downgrade; a hard billing lock
                             that blocks the chat entrance until the tier is
                             restored.

Existing already-frozen rows predate the column and their provenance is
unrecoverable, so they are backfilled to ``manual`` (the conservative,
admin-cleared-only bucket) rather than a soft reason that a chat turn could
silently thaw. Non-frozen rows keep ``NULL``.

Revision ID: c1a4d7e80303
Revises: b8d2e4f60302
Create Date: 2026-07-10 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c1a4d7e80303"
down_revision: Union[str, None] = "b8d2e4f60302"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "frozen_reason",
            sa.String(length=32),
            nullable=True,
        ),
    )
    # Source of pre-existing freezes is unrecoverable → bucket them as
    # ``manual`` so a chat turn can never silently thaw them.
    op.execute(
        "UPDATE characters SET frozen_reason = 'manual' "
        "WHERE frozen = true",
    )


def downgrade() -> None:
    op.drop_column("characters", "frozen_reason")
