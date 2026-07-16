"""add story_arcs + story_arc_beats; make story_events.seed_id nullable + add arc_beat_id

StoryArcs are multi-week narrative spines driving a character's world.
Each beat, on its scheduled date, materializes as a ``StoryEvent`` that
shares the existing narrative pipeline (expander → persist → memorialize).

Why the story_events schema changes:
- ``seed_id`` becomes nullable so arc-driven events don't need a phantom
  StorySeed row.
- ``arc_beat_id`` column identifies events created from an arc beat.
- Exactly one of (seed_id, arc_beat_id) is non-null per row — enforced
  at the service layer, not schema (too awkward as a CHECK constraint
  given SQLAlchemy 2 + Alembic autogenerate).
- Adds a second uniqueness to prevent a beat from being materialized
  twice on the same day.

Revision ID: w5e1a8d30020
Revises: v4d0f7c20019
Create Date: 2026-04-22 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "w5e1a8d30020"
down_revision: Union[str, None] = "v4d0f7c20019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "story_arcs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column(
            "theme",
            sa.String(length=64),
            nullable=False,
            server_default="custom",
        ),
        sa.Column("start_date", sa.String(length=10), nullable=False),
        sa.Column("end_date", sa.String(length=10), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
    )
    op.create_index(
        "ix_story_arcs_character_id", "story_arcs", ["character_id"],
    )
    op.create_index(
        "ix_story_arcs_status", "story_arcs", ["status"],
    )

    op.create_table(
        "story_arc_beats",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "arc_id",
            sa.String(length=36),
            sa.ForeignKey("story_arcs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("scheduled_date", sa.String(length=10), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "tension",
            sa.String(length=32),
            nullable=False,
            server_default="setup",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("realized_event_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_story_arc_beats_arc_id", "story_arc_beats", ["arc_id"],
    )
    op.create_index(
        "ix_story_arc_beats_scheduled_date",
        "story_arc_beats",
        ["scheduled_date"],
    )

    # story_events.seed_id → nullable + new arc_beat_id column
    op.alter_column("story_events", "seed_id", existing_type=sa.String(length=36), nullable=True)
    op.add_column(
        "story_events",
        sa.Column("arc_beat_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_story_events_arc_beat_id_story_arc_beats",
        "story_events",
        "story_arc_beats",
        ["arc_beat_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_story_events_arc_beat_id", "story_events", ["arc_beat_id"],
    )
    op.create_unique_constraint(
        "uq_story_events_character_date_arc_beat",
        "story_events",
        ["character_id", "date", "arc_beat_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_story_events_character_date_arc_beat",
        "story_events",
        type_="unique",
    )
    op.drop_index("ix_story_events_arc_beat_id", table_name="story_events")
    op.drop_constraint(
        "fk_story_events_arc_beat_id_story_arc_beats",
        "story_events",
        type_="foreignkey",
    )
    op.drop_column("story_events", "arc_beat_id")
    op.alter_column(
        "story_events", "seed_id", existing_type=sa.String(length=36), nullable=False,
    )

    op.drop_index("ix_story_arc_beats_scheduled_date", table_name="story_arc_beats")
    op.drop_index("ix_story_arc_beats_arc_id", table_name="story_arc_beats")
    op.drop_table("story_arc_beats")
    op.drop_index("ix_story_arcs_status", table_name="story_arcs")
    op.drop_index("ix_story_arcs_character_id", table_name="story_arcs")
    op.drop_table("story_arcs")
