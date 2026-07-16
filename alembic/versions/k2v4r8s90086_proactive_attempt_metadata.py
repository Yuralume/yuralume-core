"""proactive attempt metadata

Revision ID: k2v4r8s90086
Revises: h8p0q6r70085
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "k2v4r8s90086"
down_revision = "h8p0q6r70085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "proactive_attempts",
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.alter_column("proactive_attempts", "metadata_json", server_default=None)


def downgrade() -> None:
    op.drop_column("proactive_attempts", "metadata_json")
