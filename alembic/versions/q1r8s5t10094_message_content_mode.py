"""Add write-time content mode to messages.

Revision ID: q1r8s5t10094
Revises: p0q7r4s10093
Create Date: 2026-06-13
"""

from alembic import op
import sqlalchemy as sa


revision = "q1r8s5t10094"
down_revision = "p0q7r4s10093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "content_mode",
            sa.String(length=16),
            nullable=False,
            server_default="normal",
        ),
    )
    op.create_index(
        "ix_messages_content_mode",
        "messages",
        ["content_mode"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_content_mode", table_name="messages")
    op.drop_column("messages", "content_mode")
