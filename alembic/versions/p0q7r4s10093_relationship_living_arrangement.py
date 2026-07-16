"""add relationship living arrangement

Revision ID: p0q7r4s10093
Revises: o9p6q3r10092
Create Date: 2026-06-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "p0q7r4s10093"
down_revision = "o9p6q3r10092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "character_operator_relationship_seeds",
        sa.Column("living_arrangement", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("character_operator_relationship_seeds", "living_arrangement")
