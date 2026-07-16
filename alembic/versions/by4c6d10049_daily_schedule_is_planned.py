"""daily_schedules.is_planned

Adds the ``is_planned`` boolean to ``daily_schedules`` so the
schedule service can distinguish a fully LLM-planned day from a row
lazy-created by a chat-extracted future commitment (e.g.
"明天 7 點看電影") that's still waiting for the next ``ensure_schedule``
pass to fold the commitment into a full plan.

Backfill is implicit: server_default=true means every existing row
becomes ``is_planned=True``, matching the historical behaviour where
any row with activities is treated as fully planned.

Revision ID: by4c6d10049
Revises: bx3b5c00048
Create Date: 2026-05-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "by4c6d10049"
down_revision: Union[str, None] = "bx3b5c00048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "daily_schedules",
        sa.Column(
            "is_planned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("daily_schedules", "is_planned")
