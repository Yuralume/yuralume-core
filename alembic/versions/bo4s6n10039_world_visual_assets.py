"""world visual assets

Revision ID: bo4s6n10039
Revises: bn3r5m00038
Create Date: 2026-05-05 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bo4s6n10039"
down_revision: Union[str, None] = "bn3r5m00038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "world_visual_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("world_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("urls_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("place_id", sa.String(length=36), nullable=True),
        sa.Column("happening_id", sa.String(length=36), nullable=True),
        sa.Column("actor_kind", sa.String(length=20), nullable=True),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column(
            "actor_name",
            sa.String(length=200),
            server_default="",
            nullable=False,
        ),
        sa.Column("label", sa.String(length=200), server_default="", nullable=False),
        sa.Column("prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("metadata_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["happening_id"], ["world_happenings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_world_visual_assets_world_id",
        "world_visual_assets",
        ["world_id"],
        unique=False,
    )
    op.create_index(
        "ix_world_visual_assets_place_id",
        "world_visual_assets",
        ["place_id"],
        unique=False,
    )
    op.create_index(
        "ix_world_visual_assets_happening_id",
        "world_visual_assets",
        ["happening_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_world_visual_assets_happening_id", table_name="world_visual_assets")
    op.drop_index("ix_world_visual_assets_place_id", table_name="world_visual_assets")
    op.drop_index("ix_world_visual_assets_world_id", table_name="world_visual_assets")
    op.drop_table("world_visual_assets")
