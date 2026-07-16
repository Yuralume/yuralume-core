"""add state_last_active_at to characters

Track when a character was last active so idle-time rest recovery
can be computed lazily on the next interaction.

Revision ID: b3d8e1f56a20
Revises: a1c7f9d42e10
Create Date: 2026-04-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b3d8e1f56a20"
down_revision: Union[str, None] = "a1c7f9d42e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("state_last_active_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("characters", "state_last_active_at")
