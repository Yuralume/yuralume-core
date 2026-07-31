"""story_seeds tier + regions structural fields.

STORY_SEED_ENRICHMENT_PLAN §1 (SE0): seeds gain two structural axes so
one table serves two pacing rhythms and regional flavour without
polluting either.

* ``tier`` — ``daily`` micro-events feed the everyday gacha; ``dramatic``
  one-line scene openers are excluded from the daily rotation and are
  consumed by the impromptu-episode layer. String column, server default
  ``daily``: every pre-existing seed is exactly what the historical,
  only tier was.
* ``regions`` — JSON list of region codes with the same JSON-in-Text
  convention as ``world_frames`` and ``global`` as the wildcard. Server
  default ``["global"]``: every pre-existing seed was drawable for
  everyone, and stays so.

Both defaults are behaviour-preserving by construction, so there is no
backfill and no index — the columns are only ever read alongside the
seed row itself during the (small, fully-scanned) pool filter.

Revision ID: z1p8i5m10027
Revises: y0n7h4l10026
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "z1p8i5m10027"
down_revision = "y0n7h4l10026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "story_seeds",
        sa.Column(
            "tier",
            sa.String(length=16),
            nullable=False,
            server_default="daily",
        ),
    )
    op.add_column(
        "story_seeds",
        sa.Column(
            "regions",
            sa.Text(),
            nullable=False,
            server_default='["global"]',
        ),
    )


def downgrade() -> None:
    op.drop_column("story_seeds", "regions")
    op.drop_column("story_seeds", "tier")
