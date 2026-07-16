"""schedule activity scene access affordance

Revision ID: d0g2h7i80077
Revises: cz1e3f60076
Create Date: 2026-05-28
"""

from alembic import op
import sqlalchemy as sa


revision = "d0g2h7i80077"
down_revision = "cz1e3f60076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedule_activities",
        sa.Column("scene_privacy", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "schedule_activities",
        sa.Column("meeting_affordance", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schedule_activities", "meeting_affordance")
    op.drop_column("schedule_activities", "scene_privacy")
