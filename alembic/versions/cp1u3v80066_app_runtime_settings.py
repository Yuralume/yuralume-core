"""app_runtime_settings — runtime-mutable per-installation KV settings

Revision ID: cp1u3v80066
Revises: co0t2u70065
Create Date: 2026-05-21 08:00:00.000000

HUMANIZATION_ROADMAP §4.5 — quiet hours window (default 02:00–06:00) is
the first key persisted here, but the table is intentionally generic so
later P2/P3 runtime-mutable knobs (priority queue ordering, latency
budgets, A/B salt) can land without further migrations.

Why a single KV table vs. a dedicated column on ``app_preferences``:
``app_preferences`` is per-character / per-operator; quiet hours and
similar are **global per-installation** (the host LM Studio has one
queue, one quiet window). Mixing them would force every reader to
filter by sentinel character_id.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cp1u3v80066"
down_revision: Union[str, None] = "co0t2u70065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_runtime_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("app_runtime_settings")
