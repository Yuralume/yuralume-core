"""add operator_profiles.display_name_locked

Lets a player-edited display name survive a cloud OAuth re-login (which
otherwise re-derives display_name from the provider identity on every
login). Existing rows backfill to ``false`` (unlocked / OAuth-synced).

Revision ID: q3b5e7s90202
Revises: p2a4d6r80201
Create Date: 2026-06-29 00:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "q3b5e7s90202"
down_revision: Union[str, None] = "p2a4d6r80201"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "operator_profiles",
        sa.Column(
            "display_name_locked",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("operator_profiles", "display_name_locked")
