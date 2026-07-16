"""Add frontier-safe summary to messages.

Revision ID: q2s9t6u10095
Revises: q1r8s5t10094
Create Date: 2026-06-13
"""

from alembic import op
import sqlalchemy as sa


revision = "q2s9t6u10095"
down_revision = "q1r8s5t10094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "safe_summary",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "safe_summary")
