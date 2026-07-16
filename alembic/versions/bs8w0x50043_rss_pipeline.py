"""rss pipeline (sources + per-character inbox + category column)

Re-introduces the external-event pipeline that was deleted along with
the deprecated world system. Three structural changes:

1. ``world_events.category`` (text, default ``"news"``) so the curator
   can pre-filter by RSS category before doing embedding similarity.
2. ``rss_sources`` table — operator-visible feed registry with health
   fields (``last_success_at`` / ``last_error``).
3. ``character_event_inbox`` table — per-character curated queue. One
   row per (character, world_event); ``claimed_by_surface`` is the
   linchpin for the no-double-use guarantee across proactive / feed /
   drama surfaces.
4. ``characters.subscribed_categories`` + ``characters.excluded_topics``
   — text-as-JSON tag lists so each character can narrow what their
   inbox curator considers.

Revision ID: bs8w0x50043
Revises: br7v9w40042
Create Date: 2026-05-08 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bs8w0x50043"
down_revision: Union[str, None] = "br7v9w40042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "world_events",
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default="news",
        ),
    )
    op.create_index(
        "ix_world_events_category",
        "world_events",
        ["category"],
    )

    op.create_table(
        "rss_sources",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("feed_url", sa.Text(), nullable=False),
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default="news",
            index=True,
        ),
        sa.Column(
            "locale",
            sa.String(length=16),
            nullable=False,
            server_default="zh-TW",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_success_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column(
            "fetched_count_total",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "default_for_categories",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )

    op.create_table(
        "character_event_inbox",
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
        sa.Column(
            "similarity",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "claimed_by_surface",
            sa.String(length=32),
            nullable=True,
        ),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Per-character event uniqueness — curator must skip events the
    # character already has in the inbox (claimed or not).
    op.create_index(
        "ix_character_event_inbox_unique",
        "character_event_inbox",
        ["character_id", "world_event_id"],
        unique=True,
    )
    # Hot path: dispenser scans for the oldest unclaimed row per
    # character. Partial index on NULL claimed_by_surface keeps it
    # cheap even as the table accumulates claimed history.
    op.create_index(
        "ix_character_event_inbox_unclaimed",
        "character_event_inbox",
        ["character_id", "created_at"],
        postgresql_where=sa.text("claimed_by_surface IS NULL"),
    )

    op.add_column(
        "characters",
        sa.Column(
            "subscribed_categories",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "excluded_topics",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "excluded_topics")
    op.drop_column("characters", "subscribed_categories")
    op.drop_index(
        "ix_character_event_inbox_unclaimed",
        table_name="character_event_inbox",
    )
    op.drop_index(
        "ix_character_event_inbox_unique",
        table_name="character_event_inbox",
    )
    op.drop_table("character_event_inbox")
    op.drop_table("rss_sources")
    op.drop_index("ix_world_events_category", table_name="world_events")
    op.drop_column("world_events", "category")
