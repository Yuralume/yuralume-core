"""rpg campaigns

Revision ID: bp5t7u20040
Revises: bo4s6n10039
Create Date: 2026-05-05 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bp5t7u20040"
down_revision: Union[str, None] = "bo4s6n10039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rpg_campaigns",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), server_default="", nullable=False),
        sa.Column("player_name", sa.String(length=120), server_default="", nullable=False),
        sa.Column(
            "current_scene_id",
            sa.String(length=120),
            server_default="",
            nullable=False,
        ),
        sa.Column("ending_state", sa.String(length=120), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rpg_campaigns_updated_at",
        "rpg_campaigns",
        ["updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_rpg_campaigns_current_scene_id",
        "rpg_campaigns",
        ["current_scene_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_rpg_campaigns_current_scene_id", table_name="rpg_campaigns")
    op.drop_index("ix_rpg_campaigns_updated_at", table_name="rpg_campaigns")
    op.drop_table("rpg_campaigns")
