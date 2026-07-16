"""world system seam — empty tables + characters.world_id

Phase 0 of ``docs/WORLD_SYSTEM_PLAN.md``. All eight new tables ship
empty; characters get a nullable ``world_id`` seam. Zero behaviour
change — the world layer is opt-in and characters not assigned to any
world keep working exactly like today.

Naming note: the existing ``world_events`` table (RSS news pool, see
revision q8e5c2d10014) is left untouched. World-internal happenings
go to a new ``world_happenings`` table to avoid colliding with the
news ingest pipeline. ``memory_items.world_id`` is already a reserved
seam (revision bh7l9g50032), no alter required here.

Revision ID: bk0o2j80035
Revises: bj9n1i70034
Create Date: 2026-05-02 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bk0o2j80035"
down_revision: Union[str, None] = "bj9n1i70034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worlds",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "tone", sa.String(length=40), nullable=False, server_default="daily",
        ),
        sa.Column(
            "world_arc_template_id", sa.String(length=64), nullable=True,
        ),
        sa.Column(
            "time_zone",
            sa.String(length=64),
            nullable=False,
            server_default="Asia/Tokyo",
        ),
        sa.Column(
            "weather_seed", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column(
            "setting_json", sa.Text(), nullable=False, server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "places",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "world_id",
            sa.String(length=36),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "parent_place_id",
            sa.String(length=36),
            sa.ForeignKey("places.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "description", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "place_type",
            sa.String(length=40),
            nullable=False,
            server_default="generic",
        ),
        sa.Column(
            "kind_tags", sa.Text(), nullable=False, server_default="[]",
        ),
    )
    op.create_index(
        "ix_places_world_id_parent",
        "places",
        ["world_id", "parent_place_id"],
    )

    op.create_table(
        "place_adjacencies",
        sa.Column(
            "from_place_id",
            sa.String(length=36),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "to_place_id",
            sa.String(length=36),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "travel_minutes",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "mode", sa.String(length=20), nullable=False, server_default="walk",
        ),
    )

    op.create_table(
        "npcs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "world_id",
            sa.String(length=36),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "summary", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "personality_tags",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "default_place_id",
            sa.String(length=36),
            sa.ForeignKey("places.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "schedule_pattern_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )

    op.create_table(
        "world_inhabitants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "world_id",
            sa.String(length=36),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("ref_id", sa.String(length=36), nullable=False),
        sa.Column(
            "current_place_id",
            sa.String(length=36),
            sa.ForeignKey("places.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("intent_text", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "world_id",
            "kind",
            "ref_id",
            name="uq_world_inhabitants_ref",
        ),
    )
    op.create_index(
        "ix_world_inhabitants_world_place",
        "world_inhabitants",
        ["world_id", "current_place_id"],
    )

    op.create_table(
        "world_presences",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "world_id",
            sa.String(length=36),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operator_id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "persona_text", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "current_place_id",
            sa.String(length=36),
            sa.ForeignKey("places.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "last_moved_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "world_id",
            "operator_id",
            name="uq_world_presences_operator",
        ),
    )

    op.create_table(
        "world_happenings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "world_id",
            sa.String(length=36),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column(
            "place_id",
            sa.String(length=36),
            sa.ForeignKey("places.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "visibility_rule",
            sa.String(length=20),
            nullable=False,
            server_default="participants",
        ),
        sa.Column(
            "participants_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default="overseer",
        ),
        sa.Column(
            "metadata_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_index(
        "ix_world_happenings_world_time",
        "world_happenings",
        ["world_id", "occurred_at"],
    )

    op.create_table(
        "known_facts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "world_id",
            sa.String(length=36),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("holder_kind", sa.String(length=20), nullable=False),
        sa.Column("holder_id", sa.String(length=36), nullable=False),
        sa.Column("fact_text", sa.Text(), nullable=False),
        sa.Column(
            "source_event_id",
            sa.String(length=36),
            sa.ForeignKey("world_happenings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "certainty",
            sa.Float(),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column(
            "tags", sa.Text(), nullable=False, server_default="[]",
        ),
    )
    op.create_index(
        "ix_known_facts_holder",
        "known_facts",
        ["world_id", "holder_kind", "holder_id", "learned_at"],
    )

    op.add_column(
        "characters",
        sa.Column("world_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_characters_world_id", "characters", ["world_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_characters_world_id", table_name="characters")
    op.drop_column("characters", "world_id")
    op.drop_index("ix_known_facts_holder", table_name="known_facts")
    op.drop_table("known_facts")
    op.drop_index(
        "ix_world_happenings_world_time", table_name="world_happenings",
    )
    op.drop_table("world_happenings")
    op.drop_table("world_presences")
    op.drop_index(
        "ix_world_inhabitants_world_place", table_name="world_inhabitants",
    )
    op.drop_table("world_inhabitants")
    op.drop_table("npcs")
    op.drop_table("place_adjacencies")
    op.drop_index("ix_places_world_id_parent", table_name="places")
    op.drop_table("places")
    op.drop_table("worlds")
