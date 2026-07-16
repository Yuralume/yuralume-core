"""default new characters to proactive messaging on

Revision ID: e2i4j9k00079
Revises: d1h3i8j90078
Create Date: 2026-05-31
"""

from alembic import op
import sqlalchemy as sa


revision = "e2i4j9k00079"
down_revision = "d1h3i8j90078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "characters",
        "proactive_enabled",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.true(),
    )


def downgrade() -> None:
    op.alter_column(
        "characters",
        "proactive_enabled",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    )
