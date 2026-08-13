"""character_event_mentions — events the character already raised.

The event inbox records what a character *could* bring up, and a row is
claimed the moment a surface consumes it — after which the chat peek can
never see it again. So a proactive DM about a news item made that item
disappear from chat context at the instant it was sent, and the player's
follow-up question had nothing behind it. This table records the other
half: which world events this character actually said out loud, so chat
can put them back in front of the model (with their original link) when
the player asks.

Additive, empty on upgrade, no data change — safe to roll back. Both
foreign keys cascade so a deleted character or a GC'd world event takes
its mentions with it on PostgreSQL; self-host SQLite does not enforce
foreign keys, so the repository also sweeps by age.

Revision ID: i1z8t5w10037
Revises: h0y7s4v10036
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "i1z8t5w10037"
down_revision = "h0y7s4v10036"
branch_labels = None
depends_on = None


_TABLE = "character_event_mentions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "world_event_id",
            sa.String(length=36),
            sa.ForeignKey("world_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("surface", sa.String(length=32), nullable=False),
        sa.Column("mentioned_at", sa.DateTime(timezone=True), nullable=False),
    )
    # One relationship per (character, event): raising the same item twice
    # refreshes the timestamp instead of adding a row. This index is the
    # upsert's conflict target, not just a constraint.
    op.create_index(
        f"ix_{_TABLE}_unique", _TABLE, ["character_id", "world_event_id"],
        unique=True,
    )
    # The chat read: this character's most recent mentions.
    op.create_index(
        f"ix_{_TABLE}_recent", _TABLE, ["character_id", "mentioned_at"],
    )


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_recent", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_unique", table_name=_TABLE)
    op.drop_table(_TABLE)
