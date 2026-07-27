"""story_arcs — at most one active arc per character (cross-replica fix).

``StoryArcService.ensure_active_arc`` guarded "one active arc per character"
with a process-local ``asyncio.Lock`` plus a double-check. Under the hosted
topology (``core-router`` load-balancing 2× api, plus 2× worker) that lock is
not mutual exclusion: two replicas both read "no active arc", both pass the
double-check, both run a full multi-call LLM arc plan and both INSERT an active
row. ``get_active_for_character`` orders by ``updated_at DESC LIMIT 1``, so from
then on the narrative spine flip-flops between the two arcs depending on which
was touched last.

This revision installs the DB-level backstop: a *partial* unique index over
``character_id`` restricted to ``status = 'active'``. Partial indexes are
supported by both PostgreSQL and SQLite, so the same DDL covers the hosted
deployment and the self-host/test SQLite path. Completed / abandoned arcs stay
unconstrained — a character keeps its full arc history.

The index cannot be created while duplicates exist, and the bug has been live,
so the upgrade first converges any character that already owns >1 active arc:
keep the most recently updated one (the row the read path was already
returning) and mark the rest ``completed``. ``completed`` rather than
``abandoned`` because these arcs really did run — the losing arc's realized
beats are genuine history and its pending beats are simply no longer scheduled;
``abandoned`` additionally means "the operator/service threw this away", which
is not what happened here.

Revision ID: s4q1r6t7020
Revises: r3p0q5s6019
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "s4q1r6t7020"
down_revision = "r3p0q5s6019"
branch_labels = None
depends_on = None


_INDEX_NAME = "uq_story_arcs_active_character"

# Window functions are available on PostgreSQL and on SQLite >= 3.25, which is
# well below every version this project supports, so one portable statement
# covers both dialects.
_CONVERGE_DUPLICATE_ACTIVE_SQL = sa.text(
    """
    UPDATE story_arcs
    SET status = 'completed'
    WHERE id IN (
        SELECT id
        FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY character_id
                       ORDER BY updated_at DESC, id DESC
                   ) AS duplicate_rank
            FROM story_arcs
            WHERE status = 'active'
        ) ranked
        WHERE duplicate_rank > 1
    )
    """
)


def upgrade() -> None:
    # Must precede the index or a deployment carrying live duplicates fails.
    op.execute(_CONVERGE_DUPLICATE_ACTIVE_SQL)
    op.create_index(
        _INDEX_NAME,
        "story_arcs",
        ["character_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    # The convergence is not reversed: the losing arcs were genuinely
    # superseded and re-activating them would recreate the split spine.
    op.drop_index(_INDEX_NAME, table_name="story_arcs")
