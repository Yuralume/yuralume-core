"""add memory_items.participants_json + world_id + location

Phase 2 of the world-system roadmap (see ``docs/TODO.md`` §🟣).
The post-turn extractor now records every named person as a
structured ``ParticipantRef`` so cross-character prompts can later
disambiguate "他/她" without re-running NLP. ``world_id`` and
``location`` are both nullable seams reserved for the eventual
multi-world / Place system; populating them never breaks today's
behaviour because no read path filters on them yet.

Existing rows backfill to:
- ``participants_json = '[]'`` (no recorded participants)
- ``world_id = NULL``
- ``location = NULL``

The ``participants_json`` server_default keeps the column NOT NULL —
the read path never has to handle SQL NULL for that field.

Revision ID: bh7l9g50032
Revises: bg6k8f40031
Create Date: 2026-04-30 12:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bh7l9g50032"
down_revision: Union[str, None] = "bg6k8f40031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column(
            "participants_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "memory_items",
        sa.Column("world_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("location", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_items", "location")
    op.drop_column("memory_items", "world_id")
    op.drop_column("memory_items", "participants_json")
