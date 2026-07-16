"""add daily_schedules and schedule_activities tables

Introduces character daily schedules — a lazy-generated plan for what the
character is doing at each time of day. Tables mirror the domain split:
one row per (character, civil date) plus a child row per activity block.

Revision ID: f7b5c8e20003
Revises: e6a4b7d10002
Create Date: 2026-04-17 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f7b5c8e20003"
down_revision: Union[str, None] = "e6a4b7d10002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("character_id", sa.String(36), nullable=False, index=True),
        sa.Column("date", sa.String(10), nullable=False, index=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_daily_schedules_character_date",
        "daily_schedules",
        ["character_id", "date"],
        unique=True,
    )
    op.create_table(
        "schedule_activities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "schedule_id",
            sa.String(36),
            sa.ForeignKey("daily_schedules.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("schedule_activities")
    op.drop_index("ix_daily_schedules_character_date", table_name="daily_schedules")
    op.drop_table("daily_schedules")
