"""turn record operator feedback

Revision ID: n4y6t2u10088
Revises: l3x5s0t90087
Create Date: 2026-06-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "n4y6t2u10088"
down_revision = "l3x5s0t90087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "turn_records",
        sa.Column(
            "operator_feedback",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.alter_column("turn_records", "operator_feedback", server_default=None)


def downgrade() -> None:
    op.drop_column("turn_records", "operator_feedback")
