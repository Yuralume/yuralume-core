"""add proactive messaging schema

* ``characters`` gains ``proactive_enabled`` / ``proactive_daily_limit``
  / ``proactive_cooldown_minutes``.
* ``channel_bindings`` gains ``accepts_proactive``.
* New ``proactive_attempts`` audit-log table (one row per evaluation,
  including gate-blocked and decider-skipped cases).

All new flags default off so existing characters don't start messaging
automatically after the upgrade.

Revision ID: m4a1c8e70010
Revises: l3f0b7e60009
Create Date: 2026-04-18 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "m4a1c8e70010"
down_revision: Union[str, None] = "l3f0b7e60009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "proactive_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "proactive_daily_limit",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "proactive_cooldown_minutes",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )

    op.add_column(
        "channel_bindings",
        sa.Column(
            "accepts_proactive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "proactive_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False, index=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "binding_id",
            sa.String(length=36),
            sa.ForeignKey("channel_bindings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("proactive_attempts")
    op.drop_column("channel_bindings", "accepts_proactive")
    op.drop_column("characters", "proactive_cooldown_minutes")
    op.drop_column("characters", "proactive_daily_limit")
    op.drop_column("characters", "proactive_enabled")
