"""schedule_activities.source_beat_id — KB2 of PLAYER_KNOWLEDGE_BOUNDARY_PLAN

The schedule planner reserves a block for the arc beat scheduled that
day. That block is a *plan for a scene*, and the scene's own record is
written when the beat is realized — so the memorializer has to be able
to tell it apart from a lived hour and skip it. Before this column it
could not: a beat the player was central to was staged as a solo day and
became an episodic memory of an event the player never took part in.

Nullable, no server default, zero backfill (D7 of that plan): ``NULL``
means "an ordinary activity", which is the honest reading of every
pre-migration row — the lineage simply was not recorded then, and
guessing it from activity text is exactly the keyword sniffing the plan's
LLM-first red line forbids.

Revision ID: y5m1g8j10050
Revises: x4l0f7i10049
Create Date: 2026-08-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "y5m1g8j10050"
down_revision: Union[str, None] = "x4l0f7i10049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schedule_activities",
        sa.Column("source_beat_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schedule_activities", "source_beat_id")
