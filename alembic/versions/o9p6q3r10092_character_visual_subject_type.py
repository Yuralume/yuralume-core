"""add character visual subject type

Revision ID: o9p6q3r10092
Revises: n8e5g2i10091
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "o9p6q3r10092"
down_revision = "n8e5g2i10091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "visual_subject_type",
            sa.String(length=32),
            nullable=False,
            server_default="auto",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "visual_subject_type")
