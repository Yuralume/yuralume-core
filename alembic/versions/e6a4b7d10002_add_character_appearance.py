"""add characters.appearance

Adds a free-text appearance description field to the ``characters``
table. Defaulted to empty string so existing rows remain valid.

Revision ID: e6a4b7d10002
Revises: d5f3a6c90001
Create Date: 2026-04-17 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e6a4b7d10002"
down_revision: Union[str, None] = "d5f3a6c90001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("appearance", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("characters", "appearance")
