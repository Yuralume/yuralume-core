"""daily_schedules weather-vet marker — intra-day drift gate idempotency.

A day is planned once against the weather that was true at midnight; the
intra-day drift judge later rewrites the blocks that plainly contradict the
sky as it actually is. That judge must not re-run on every tick, and two
instances ticking the same character must not both pay for it, so the day
carries the marker of what it was last vetted against:

* ``daily_schedules.weather_vet_activity_id`` — the activity that was in
  progress at the last judgement.
* ``daily_schedules.weather_vet_condition`` — the weather condition phrase
  that was true at that same moment.

While both halves are unchanged the tick skips the call; either half moving
(the day advanced to the next block, or the weather turned mid-block)
re-opens the gate. The pair is a **cost gate only** — the judgement itself
always re-reads the full schedule text against the full weather facts.

Both columns are nullable with no server default and no backfill: every row
that exists at upgrade time has genuinely never been vetted, and ``NULL``
is exactly the value that opens the gate on the first tick afterwards.

Revision ID: y0n7h4l10026
Revises: x9m6g3k10025
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "y0n7h4l10026"
down_revision = "x9m6g3k10025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_schedules",
        sa.Column("weather_vet_activity_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "daily_schedules",
        sa.Column("weather_vet_condition", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("daily_schedules", "weather_vet_condition")
    op.drop_column("daily_schedules", "weather_vet_activity_id")
