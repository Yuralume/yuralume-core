"""operator profile current status for scene access

Revision ID: d1h3i8j90078
Revises: d0g2h7i80077
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa


revision = "d1h3i8j90078"
down_revision = "d0g2h7i80077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operator_profiles",
        sa.Column("current_status", sa.Text(), nullable=True),
    )
    op.add_column(
        "operator_profiles",
        sa.Column(
            "current_status_set_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("operator_profiles", "current_status_set_at")
    op.drop_column("operator_profiles", "current_status")
