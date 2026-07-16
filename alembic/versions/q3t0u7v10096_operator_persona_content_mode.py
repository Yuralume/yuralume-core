"""operator persona content mode

Revision ID: q3t0u7v10096
Revises: q2s9t6u10095
Create Date: 2026-06-13 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "q3t0u7v10096"
down_revision = "q2s9t6u10095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operator_profile_fields",
        sa.Column(
            "content_mode",
            sa.String(length=16),
            nullable=False,
            server_default="normal",
        ),
    )


def downgrade() -> None:
    op.drop_column("operator_profile_fields", "content_mode")
