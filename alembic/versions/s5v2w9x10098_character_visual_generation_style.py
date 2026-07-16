"""character visual generation style

Revision ID: s5v2w9x10098
Revises: r4u1v8w10097
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "s5v2w9x10098"
down_revision = "r4u1v8w10097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "visual_generation_style",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "visual_generation_style")
