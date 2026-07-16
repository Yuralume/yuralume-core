"""normalise emotion event default operator id

Revision ID: ci4n6o10059
Revises: ch3m5n00058
Create Date: 2026-05-20 18:00:00.000000

Backfill the temporary ``default-operator`` sentinel used by the first
EmotionEvent implementation to the canonical operator id ``default``.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "ci4n6o10059"
down_revision: Union[str, None] = "ch3m5n00058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE emotion_events "
        "SET operator_id = 'default' "
        "WHERE operator_id = 'default-operator'",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE emotion_events "
        "SET operator_id = 'default-operator' "
        "WHERE operator_id = 'default'",
    )
