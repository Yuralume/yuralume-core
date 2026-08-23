"""drop operator_profiles.current_status / current_status_set_at

Revision ID: p6d2x9a10041
Revises: n5c1w8z10040
Create Date: 2026-08-17

PP series — "我現在的狀態" current_status is retired in favour of the
per-(character, operator) ``player_persona_notes`` declaration
(``n5c1w8z10040``). This field only ever fed the now-retired Scene
Access judge and, after D4, a plain narrative status line; both are
gone, so the columns are dead weight.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "p6d2x9a10041"
down_revision: Union[str, None] = "n5c1w8z10040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.drop_column("current_status_set_at")
        batch_op.drop_column("current_status")


def downgrade() -> None:
    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.add_column(
            sa.Column("current_status", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column(
                "current_status_set_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
