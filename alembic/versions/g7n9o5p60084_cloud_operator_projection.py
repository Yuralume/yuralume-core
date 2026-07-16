"""cloud operator projection fields

Revision ID: g7n9o5p60084
Revises: f6m8n3o40083
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "g7n9o5p60084"
down_revision = "f6m8n3o40083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operator_profiles",
        sa.Column("cloud_account_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "operator_profiles",
        sa.Column("cloud_tenant_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "operator_profiles",
        sa.Column(
            "auth_provider",
            sa.String(length=16),
            nullable=False,
            server_default="local",
        ),
    )
    op.create_index(
        "ux_operator_profiles_cloud_account_id",
        "operator_profiles",
        ["cloud_account_id"],
        unique=True,
    )
    op.alter_column("operator_profiles", "auth_provider", server_default=None)


def downgrade() -> None:
    op.drop_index("ux_operator_profiles_cloud_account_id", table_name="operator_profiles")
    op.drop_column("operator_profiles", "auth_provider")
    op.drop_column("operator_profiles", "cloud_tenant_id")
    op.drop_column("operator_profiles", "cloud_account_id")
