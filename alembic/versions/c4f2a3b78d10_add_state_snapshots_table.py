"""add state_snapshots table

Record every state change for auditing and future visualisation.

Revision ID: c4f2a3b78d10
Revises: b3d8e1f56a20
Create Date: 2026-04-17 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4f2a3b78d10"
down_revision: Union[str, None] = "b3d8e1f56a20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "state_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("character_id", sa.String(36), nullable=False, index=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("emotion", sa.String(100), nullable=False),
        sa.Column("affection", sa.Integer, nullable=False),
        sa.Column("fatigue", sa.Integer, nullable=False),
        sa.Column("trust", sa.Integer, nullable=False),
        sa.Column("energy", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("state_snapshots")
