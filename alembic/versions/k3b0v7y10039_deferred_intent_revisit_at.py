"""deferred_intents.revisit_at — 被壓下的動機自己帶的鬧鐘 (T2).

When the intention judge holds a slot back *because a concrete future
moment is the right one* ("我們約好七點半"), it now names that moment.
The dispatcher stores it here and, once due, spends it on a single
cooldown exemption so the 19:22 tick can't blank the 19:30 window it was
waiting for.

Additive and nullable: every existing row — and every motive that has no
appointment attached, which is the ordinary case — stays NULL and
behaves exactly as before.

Revision ID: k3b0v7y10039
Revises: j2a9u6x10038
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "k3b0v7y10039"
down_revision = "j2a9u6x10038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deferred_intents",
        sa.Column(
            "revisit_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("deferred_intents", "revisit_at")
