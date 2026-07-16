"""operator_profiles.cloud_tenant_tier

Revision ID: d4e6f8a90103
Revises: c3d5e7f90102
Create Date: 2026-06-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d4e6f8a90103"
down_revision = "c3d5e7f90102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operator_profiles",
        sa.Column(
            "cloud_tenant_tier",
            sa.String(length=32),
            nullable=False,
            server_default="standard",
        ),
    )
    op.alter_column("operator_profiles", "cloud_tenant_tier", server_default=None)


def downgrade() -> None:
    op.drop_column("operator_profiles", "cloud_tenant_tier")
