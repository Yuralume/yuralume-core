"""Realtime outbox read index: (created_at, id) composite (Phase 4 §7.1).

Phase 4 activates the previously-dormant ``realtime_events`` outbox. The
``read_after`` replay read matches ``id > cursor OR created_at >= anchor -
overlap`` ordered by ``id`` — the out-of-order-commit anti-loss window. Replace
the original single-column ``ix_realtime_events_created`` with a
``(created_at, id)`` composite so the overlap-window range scan is index-served
with ``id`` covered for the ascending order; its leftmost prefix keeps the
``prune`` (``created_at < cutoff``) range covered too.

Additive/index-only, no data change; ``realtime_events`` may be empty or hold
dormant rows either way.

Revision ID: m8k5l0n1014
Revises: l7j4k9m1013
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op


revision = "m8k5l0n1014"
down_revision = "l7j4k9m1013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_realtime_events_created", table_name="realtime_events")
    op.create_index(
        "ix_realtime_events_created_id",
        "realtime_events",
        ["created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_realtime_events_created_id", table_name="realtime_events",
    )
    op.create_index(
        "ix_realtime_events_created", "realtime_events", ["created_at"],
    )
