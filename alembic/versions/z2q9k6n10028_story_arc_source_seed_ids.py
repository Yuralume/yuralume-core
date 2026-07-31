"""story_arcs.source_seed_ids — arc provenance to story seeds (AE2).

AE series (arc planner seed injection) slice AE2: ``story_arcs`` gains a
column recording which ``story_seeds.id`` values the planner folded
into this arc's premise/beats. Purely additive bookkeeping — this
migration lands the schema only; a later ticket has the LLM planner
write it and the service read it back to exclude already-used seeds
from future rolls.

``source_seed_ids`` — JSON list of seed ids, same JSON-in-Text
convention as ``StorySeedRow.regions`` / ``StoryArcBeatRow.scene_characters``
(``z1p8i5m10027`` / ``aa9e2f70025``). Server default ``'[]'``: every
pre-existing arc was planned before this field existed, so "no known
seed provenance" is the honest, behaviour-preserving value. No backfill,
no index — the column is only ever read alongside the arc row itself.

Revision ID: z2q9k6n10028
Revises: z1p8i5m10027
Create Date: 2026-07-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "z2q9k6n10028"
down_revision = "z1p8i5m10027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "story_arcs",
        sa.Column(
            "source_seed_ids",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("story_arcs", "source_seed_ids")
