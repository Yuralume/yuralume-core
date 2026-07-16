"""character_relationships table

First-class directed edge between two characters: "A 對 B 的看法是
青梅竹馬，好感 70 / 信任 80 / 緊張 10"。Distinct from the KnownFact
log because:

- Relationships are *standing state*, not events. They live across
  many encounters and decay slowly, like character.state does.
- They're prompt-grade: the chat builder injects "你對 X 的好感是 70,
  視 X 為青梅竹馬" so the model frames replies appropriately.
- Direction matters — A may pine for B while B treats A as a friend.

``world_id`` is nullable on purpose:
- ``NULL`` → global edge (applies wherever the two characters are).
- ``<world-id>`` → world-scoped edge (the same pair can hold a
  different relationship in another world; modern!Tokyo coworkers,
  fantasy!battlefield rivals).

Composite unique on (world_id, from_character_id, to_character_id)
keeps "A→B in world W" deduplicated. ``COALESCE(world_id, '')`` is
*not* used in the constraint; SQL treats NULL as distinct from NULL
in unique constraints, but that's fine — we'd never want to merge
two NULL rows because there's a service-layer rule one global edge
per (from, to) pair.

Revision ID: bm2q4l00037
Revises: bl1p3k90036
Create Date: 2026-05-02 19:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bm2q4l00037"
down_revision: Union[str, None] = "bl1p3k90036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "character_relationships",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "world_id",
            sa.String(length=36),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "from_character_id",
            sa.String(length=36),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "to_character_id",
            sa.String(length=36),
            nullable=False,
            index=True,
        ),
        sa.Column("label", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("affection", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("trust", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("tension", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "world_id",
            "from_character_id",
            "to_character_id",
            name="uq_character_relationships_edge",
        ),
    )
    op.create_index(
        "ix_character_relationships_world_from",
        "character_relationships",
        ["world_id", "from_character_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_character_relationships_world_from",
        table_name="character_relationships",
    )
    op.drop_table("character_relationships")
