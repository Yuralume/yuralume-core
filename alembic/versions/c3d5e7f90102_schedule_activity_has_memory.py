"""Add precise schedule activity memory badge flag.

Revision ID: c3d5e7f90102
Revises: b2c4d6e80101
Create Date: 2026-06-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c3d5e7f90102"
down_revision = "b2c4d6e80101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedule_activities",
        sa.Column(
            "has_memory",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Legacy rows only had ``memorialized``. Preserve the old UI promise
    # approximately for existing data; new writes set this flag precisely.
    op.execute(
        "UPDATE schedule_activities SET has_memory = memorialized "
        "WHERE memorialized = true",
    )


def downgrade() -> None:
    op.drop_column("schedule_activities", "has_memory")
