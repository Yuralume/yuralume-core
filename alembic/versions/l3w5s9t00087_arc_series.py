"""arc series

Revision ID: l3w5s9t00087
Revises: k2v4r8s90086
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "l3w5s9t00087"
down_revision = "k2v4r8s90086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("arc_series_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_characters_arc_series_id",
        "characters",
        ["arc_series_id"],
    )

    op.create_table(
        "arc_series",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("pack_id", sa.String(length=128), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column("theme", sa.String(length=64), nullable=False),
        sa.Column("tone", sa.String(length=64), nullable=False),
        sa.Column("world_frames_json", sa.Text(), nullable=False),
        sa.Column("required_traits_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["operator_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_arc_series_user_id", "arc_series", ["user_id"])
    op.create_index("ix_arc_series_pack_id", "arc_series", ["pack_id"])

    op.create_table(
        "arc_series_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("series_id", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["arc_series.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "series_id",
            "template_id",
            name="uq_arc_series_members_series_template",
        ),
        sa.UniqueConstraint(
            "series_id",
            "position",
            name="uq_arc_series_members_series_position",
        ),
    )
    op.create_index(
        "ix_arc_series_members_series_id",
        "arc_series_members",
        ["series_id"],
    )

    op.create_table(
        "character_series_progress",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("series_id", sa.String(length=64), nullable=False),
        sa.Column("current_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_arc_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["arc_series.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "character_id",
            "series_id",
            name="uq_character_series_progress_character_series",
        ),
    )
    op.create_index(
        "ix_character_series_progress_character_id",
        "character_series_progress",
        ["character_id"],
    )
    op.create_index(
        "ix_character_series_progress_series_id",
        "character_series_progress",
        ["series_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_character_series_progress_series_id",
        table_name="character_series_progress",
    )
    op.drop_index(
        "ix_character_series_progress_character_id",
        table_name="character_series_progress",
    )
    op.drop_table("character_series_progress")

    op.drop_index(
        "ix_arc_series_members_series_id",
        table_name="arc_series_members",
    )
    op.drop_table("arc_series_members")

    op.drop_index("ix_arc_series_pack_id", table_name="arc_series")
    op.drop_index("ix_arc_series_user_id", table_name="arc_series")
    op.drop_table("arc_series")

    op.drop_index("ix_characters_arc_series_id", table_name="characters")
    op.drop_column("characters", "arc_series_id")
