"""pending_follow_ups.kind + promise_intent

Adds two columns to ``pending_follow_ups`` so the table can host the
new ``scheduled_promise`` flavour (used when the user explicitly asks
the character to message them at a future time, e.g. "明天 10 點叫我
起床") alongside the original ``busy_defer`` rows.

* ``kind`` (string, default ``'busy_defer'``) — discriminator. New
  enum-like values can be introduced without a migration since the
  column is a free-form string.
* ``promise_intent`` (text, default ``''``) — natural-language
  description of what the character promised to do at ``scheduled_for``.
  Empty string for legacy ``busy_defer`` rows.

server_default values mean every existing row backfills to the legacy
shape on upgrade, so no data migration is required.

Revision ID: bz5d7e20050
Revises: by4c6d10049
Create Date: 2026-05-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bz5d7e20050"
down_revision: Union[str, None] = "by4c6d10049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pending_follow_ups",
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="busy_defer",
        ),
    )
    op.add_column(
        "pending_follow_ups",
        sa.Column(
            "promise_intent",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("pending_follow_ups", "promise_intent")
    op.drop_column("pending_follow_ups", "kind")
