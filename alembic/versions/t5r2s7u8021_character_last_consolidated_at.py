"""characters.last_consolidated_at — cross-replica consolidation claim.

``AutoConsolidationTrigger`` (post-turn memory decay + merge) gated itself with
three *per-process* structures: a ``_last_run_at`` dict for the 6h cooldown, a
``_running`` set for single-flight and a per-character ``asyncio.Lock``. The
trigger is fired fire-and-forget from the chat turn, which the hosted topology
serves from 2× api replicas, so none of the three is mutual exclusion: both
replicas could run ``consolidate()`` — an LLM-heavy pass that rewrites memory
rows — for the same character simultaneously and overwrite each other, and the
cooldown's real-world period was halved per added replica.

This column is the cross-instance claim stamp. The trigger now runs one
conditional UPDATE (``SET last_consolidated_at = <db now> WHERE id = :id AND
(last_consolidated_at IS NULL OR last_consolidated_at < :cutoff)``) and only
proceeds when it changed a row, so a single statement the DB serialises decides
cooldown *and* single-flight at once.

Nullable with no backfill: NULL reads as "never consolidated", which is the
correct starting point for every existing character (the previous state lived
only in process memory and is already gone at deploy time). Note the claim is
consumed on *attempt*, not on success — matching the pre-existing behaviour of
stamping ``_last_run_at`` before running — so a crashing consolidation cannot
be retried in a hot loop.

Revision ID: t5r2s7u8021
Revises: s4q1r6t7020
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "t5r2s7u8021"
down_revision = "s4q1r6t7020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "last_consolidated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "last_consolidated_at")
