"""daily_schedules.manually_adjusted — operator ownership of a hand-edited day.

Every pre-planned day row has ``generated_at`` on an earlier local day, so
the same-day weather refresh (``_is_stale_current_day_plan``) rebuilt the
whole day from the planner the moment the day arrived — silently discarding
any add / modify / delete the operator had made through the manual schedule
routes and "resurrecting" deleted activities. This flag records that the
operator has taken ownership of the day; the refresh skips such days and the
explicit regenerate route stays the operator-controlled way to re-open one
(regenerate replaces the row, so the flag naturally resets to false).

Backfill is implicit: server_default=false means every existing row keeps
the historical behaviour — no operator has hand-edited it as far as the
system can know, so the weather refresh continues to apply.

Revision ID: u1i7c4f10046
Revises: t0h6b3e10045
Create Date: 2026-08-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "u1i7c4f10046"
down_revision: Union[str, None] = "t0h6b3e10045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "daily_schedules",
        sa.Column(
            "manually_adjusted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("daily_schedules", "manually_adjusted")
