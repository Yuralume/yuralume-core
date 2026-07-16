"""notification_preferences.studio_enabled — Creator Studio completion push

Revision ID: g2e9c4d60708
Revises: f1d8b3c50607
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "g2e9c4d60708"
down_revision = "f1d8b3c50607"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_preferences",
        sa.Column(
            "studio_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("notification_preferences", "studio_enabled")
