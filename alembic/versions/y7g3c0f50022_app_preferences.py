"""add app_preferences kv table

Schema-less key/value store for global UI preferences (provider +
model pick first, future knobs land without a migration each). Yuralume
is single-operator so this doubles as user prefs.

Revision ID: y7g3c0f50022
Revises: x6f2b9e40021
Create Date: 2026-04-24 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "y7g3c0f50022"
down_revision: Union[str, None] = "x6f2b9e40021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_preferences",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_preferences")
