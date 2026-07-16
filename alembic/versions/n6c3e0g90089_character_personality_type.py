"""add character personality_type_json

Revision ID: n6c3e0g90089
Revises: n4y6t2u10088
Create Date: 2026-06-11 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "n6c3e0g90089"
down_revision: Union[str, None] = "n4y6t2u10088"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "personality_type_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "personality_type_json")
