"""lower schedule busy_score server default

Revision ID: n8e5g2i10091
Revises: n7d4f1h00090
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "n8e5g2i10091"
down_revision: Union[str, None] = "n7d4f1h00090"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "schedule_activities",
        "busy_score",
        existing_type=sa.Float(),
        server_default="0.4",
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "schedule_activities",
        "busy_score",
        existing_type=sa.Float(),
        server_default="0.5",
        existing_nullable=False,
    )
