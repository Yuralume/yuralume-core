"""External proactive event conversation-recorded lifecycle (M8).

``accepted`` marks that the channel took the proactive push; it does NOT mean
the delivered turn was appended to the character's ``source="line"``
conversation history — that append was previously fire-and-forget and silently
lost on a crash. This additive boolean gives the accepted send and the history
row one shared lifecycle: an ``accepted`` row still ``conversation_recorded =
FALSE`` is the retry worker's worklist for an idempotent history re-append keyed
by ``event_id``.

Backward compatible: NEW rows default to ``FALSE`` (server_default) and are
swept / re-appended on the next retry tick. But rows that were already
``accepted`` BEFORE this column existed were delivered under the pre-M8 path and
their history append was already done inline — a blanket ``FALSE`` would make
the very first post-upgrade sweep re-append every one of them. The one-shot data
migration below stamps those pre-existing ``accepted`` rows ``TRUE`` (treat a
pre-upgrade event as already recorded — prefer under-appending over duplicating);
only genuinely new post-upgrade sends flow through the sweep.

Revision ID: q2o9p4r5018
Revises: p1n8o3q4017
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "q2o9p4r5018"
down_revision = "p1n8o3q4017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "external_proactive_events",
        sa.Column(
            "conversation_recorded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # One-shot backfill: any row already ``accepted`` predates this lifecycle
    # flag, so treat it as already recorded and keep the first sweep from
    # re-appending it. New rows keep the ``FALSE`` server_default.
    events = sa.table(
        "external_proactive_events",
        sa.column("state", sa.String()),
        sa.column("conversation_recorded", sa.Boolean()),
    )
    op.execute(
        events.update()
        .where(events.c.state == "accepted")
        .values(conversation_recorded=True),
    )


def downgrade() -> None:
    op.drop_column("external_proactive_events", "conversation_recorded")
