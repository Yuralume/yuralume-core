"""worlds: add display_calendar + weather_json

Two seams that the API / service / YAML already accepted but had no
column to land on:

- ``display_calendar``: free-form fictional epoch label shown alongside
  the wall-clock datetime in prompts. Was being written into
  ``WorldClock.display_calendar`` at API level but the SA repo had no
  place for it, so it silently dropped.
- ``weather_json``: JSON-encoded :class:`Weather` snapshot. The
  overseer's ``OverseerDecision.new_weather`` field had no persistence
  endpoint, so weather was effectively a prompt-time guess. With this
  column the world now carries a real weather state the prompt builder
  can render.

Revision ID: bl1p3k90036
Revises: bk0o2j80035
Create Date: 2026-05-02 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bl1p3k90036"
down_revision: Union[str, None] = "bk0o2j80035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "worlds",
        sa.Column(
            "display_calendar",
            sa.String(length=120),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "worlds",
        sa.Column(
            "weather_json",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("worlds", "weather_json")
    op.drop_column("worlds", "display_calendar")
