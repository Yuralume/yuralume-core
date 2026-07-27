"""rss region dimension — per-player world events (HOSTED_PLAYER_GEO G3).

``rss_sources`` is a single site-wide pool: every character, whoever owns
it, is curated against the feeds the site admin happens to have enabled.
On a hosted deployment serving players worldwide that means a Japanese
player's character reads Taiwanese local news. Owner-ratified plan A adds
a **region tag on the source pool** plus a curator-side filter, rather
than per-player feed subscriptions (which would hand feed management to
non-technical players).

Two additive nullable columns, no backfill:

* ``rss_sources.region`` — ISO-ish region code (``TW`` / ``JP`` / ``US``).
  ``NULL`` means *global*: relevant to everyone. Existing self-host rows
  stay ``NULL``, so self-host behaviour is unchanged by construction.
* ``world_events.source_region`` — the owning source's region, denormalised
  onto the event at ingest time. The pool table is queried on every
  curator pass (window + category + region) and the join would be on the
  hot path for no benefit; the value is immutable for a given event, so
  the copy cannot drift.

The existing ``region`` YAML key that gates region-bound *emergency*
feeds against the deployment region is a seed-time-only concept and is
untouched — this column is the persisted, admin-editable pool dimension.

Revision ID: v7t4u9w0023
Revises: u6s3t8v9022
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "v7t4u9w0023"
down_revision = "u6s3t8v9022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rss_sources",
        sa.Column("region", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "world_events",
        sa.Column("source_region", sa.String(length=16), nullable=True),
    )
    op.create_index(
        "ix_world_events_source_region",
        "world_events",
        ["source_region"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_world_events_source_region",
        table_name="world_events",
    )
    op.drop_column("world_events", "source_region")
    op.drop_column("rss_sources", "region")
