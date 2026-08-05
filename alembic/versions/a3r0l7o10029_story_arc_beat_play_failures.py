"""story_arc_beats — split the failure counters out of the attempt counter (SC0).

``BeatRetryPolicy`` means "stop after N failed autonomous scene runs", but
it was reading ``play_attempt_count`` / ``last_play_attempt_at``, which
every staging path bumps — including the ``chat_scene_directive`` /
``prompted`` write a player triggers by simply chatting. A beat whose
scene writer never failed once could therefore exhaust its retry budget,
and any attempt at all pushed the backoff window out
(BACKGROUND_COST_CONTROL_PLAN §10 #3).

This revision adds the two columns the budget actually needs:

- ``play_failure_count`` — NOT NULL, server default ``0``
- ``last_play_failure_at`` — nullable, the backoff anchor

**Backfill: at most one provable failure per row.** The old schema cannot
say how many of a beat's attempts failed; the single fact it does carry is
"the most recent attempt was ``<result>`` at ``last_play_attempt_at``". So
rows whose last attempt was a failure land on ``play_failure_count = 1``
with that timestamp as the anchor; every other row lands on ``0`` / NULL.

The two rejected alternatives, and why:

- *Copy ``play_attempt_count`` into ``play_failure_count``* inherits the
  exact inflation this fix removes — and because SC0 also retires
  exhausted beats to ``skipped``, the first tick after deploy would
  permanently kill live story beats that never failed. Destructive, on
  data known to be wrong.
- *Zero everything* discards the one true failure and lets a genuinely
  broken beat retry immediately on the next tick, i.e. a small retry
  storm exactly where the writer is already failing.

Landing on 1 keeps the initial backoff for a beat that just failed while
leaving it ``max_attempts - 1`` tries — fail-open on budget, honest on
timing.

Revision ID: a3r0l7o10029
Revises: z2q9k6n10028
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a3r0l7o10029"
down_revision = "z2q9k6n10028"
branch_labels = None
depends_on = None


# Mirrors ``FAILED_PLAY_RESULTS`` in
# ``kokoro_link.domain.entities.story_arc``. Duplicated on purpose:
# migrations are historical artifacts and must keep meaning the same
# thing even after the domain vocabulary grows.
_BACKFILL_LAST_FAILURE_SQL = sa.text(
    """
    UPDATE story_arc_beats
    SET play_failure_count = 1,
        last_play_failure_at = last_play_attempt_at
    WHERE last_play_attempt_at IS NOT NULL
      AND last_play_attempt_result IN ('failed', 'writer_crashed', 'empty_scene')
    """
)


def upgrade() -> None:
    op.add_column(
        "story_arc_beats",
        sa.Column(
            "play_failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column("last_play_failure_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(_BACKFILL_LAST_FAILURE_SQL)


def downgrade() -> None:
    op.drop_column("story_arc_beats", "last_play_failure_at")
    op.drop_column("story_arc_beats", "play_failure_count")
