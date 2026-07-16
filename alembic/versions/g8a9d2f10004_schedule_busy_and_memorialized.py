"""add busy_score and memorialized to schedule_activities

Phase 2 enrichments:
- ``busy_score`` lets the prompt builder modulate reply tone based on
  how engrossed the character is in their current activity.
- ``memorialized`` records whether a completed block has been turned
  into an episodic memory, so the memorializer runs exactly once.

Revision ID: g8a9d2f10004
Revises: f7b5c8e20003
Create Date: 2026-04-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "g8a9d2f10004"
down_revision: Union[str, None] = "f7b5c8e20003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schedule_activities",
        sa.Column("busy_score", sa.Float(), nullable=False, server_default="0.5"),
    )
    op.add_column(
        "schedule_activities",
        sa.Column(
            "memorialized", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("schedule_activities", "memorialized")
    op.drop_column("schedule_activities", "busy_score")
