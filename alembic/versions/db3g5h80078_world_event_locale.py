"""add world event source locale

Revision ID: db3g5h80078
Revises: da2f4g70077
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "db3g5h80078"
down_revision = "da2f4g70077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "world_events",
        sa.Column("locale", sa.String(length=16), nullable=True),
    )
    op.create_index("ix_world_events_locale", "world_events", ["locale"])


def downgrade() -> None:
    op.drop_index("ix_world_events_locale", table_name="world_events")
    op.drop_column("world_events", "locale")
