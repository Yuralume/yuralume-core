"""add aspirations, current_intent, and character_goals table

Adds short-/medium-/long-term goal structures:
- ``characters.aspirations`` — long-term (static, profile-editable)
- ``characters.state_current_intent`` — short-term (revised each turn)
- ``character_goals`` table — medium-term (reviewed periodically)

Revision ID: d5f3a6c90001
Revises: c4f2a3b78d10
Create Date: 2026-04-17 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d5f3a6c90001"
down_revision: Union[str, None] = "c4f2a3b78d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("aspirations", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "characters",
        sa.Column("state_current_intent", sa.Text(), nullable=True),
    )
    op.create_table(
        "character_goals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("character_id", sa.String(36), nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active", index=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("origin", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_progressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("character_goals")
    op.drop_column("characters", "state_current_intent")
    op.drop_column("characters", "aspirations")
