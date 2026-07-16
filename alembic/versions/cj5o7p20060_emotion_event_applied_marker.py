"""emotion event applied-to-state marker

Revision ID: cj5o7p20060
Revises: ci4n6o10059
Create Date: 2026-05-20 20:30:00.000000

Mark legacy emotion events whose numeric deltas were already mirrored
into ``characters`` flat state columns. The full derived read model can
then skip those numeric deltas while still using the event for
provenance, labels, valence/arousal, and dashboard timelines.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cj5o7p20060"
down_revision: Union[str, None] = "ci4n6o10059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "emotion_events",
        sa.Column(
            "applied_to_state",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("emotion_events", "applied_to_state")
