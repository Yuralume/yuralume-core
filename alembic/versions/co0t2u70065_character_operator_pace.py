"""characters.operator_pace_preference — operator dialogue pace knob

Revision ID: co0t2u70065
Revises: cn9s1t60064
Create Date: 2026-05-21 06:00:00.000000

HUMANIZATION_ROADMAP §3.6 — operator-facing per-character dialogue
pace preference (``more_active`` / ``balanced`` / ``more_quiet``).
Empty string = unset → no prompt injection (legacy behaviour preserved).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "co0t2u70065"
down_revision: Union[str, None] = "cn9s1t60064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "operator_pace_preference",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "operator_pace_preference")
