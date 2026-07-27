"""rss_sources.region_locked — make "clear region to global" durable.

``rss_sources.region`` is admin-editable but also seeded from
``data/rss_sources.yaml`` on every boot. The seed backfills a region only
while the row has none, which reads ``NULL`` as "never tagged" — correct
for rows predating ``v7t4u9w0023``, wrong for a row an operator
deliberately cleared back to *global*: the YAML value came straight back
on the next start and the feed silently resumed leaking into one region's
pool only.

One additive boolean carries the missing intent:

* ``rss_sources.region_locked`` — ``TRUE`` once the admin API has
  explicitly set *or cleared* ``region``. The seed backfills only while it
  is ``FALSE``.

``NOT NULL DEFAULT FALSE`` with no backfill: every row that exists at
upgrade time is by definition one the new admin path has never written, so
"unlocked" is both the honest value and the one that preserves the current
upgrade behaviour (a self-host that upgraded before the region column
existed still picks the shipped regions up on its next sync).

Revision ID: x9m6g3k10025
Revises: w8k5f2j10024
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "x9m6g3k10025"
down_revision = "w8k5f2j10024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rss_sources",
        sa.Column(
            "region_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("rss_sources", "region_locked")
