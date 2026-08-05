"""memory_items — composite index for keyset browse pagination (IV8).

``memory_items`` carried three indexes before this: ``character_id``,
``kind``, and two HNSW vector indexes. **Nothing on ``created_at``.**

That was survivable only because the browse endpoint had no ``LIMIT`` —
it read every one of a character's rows anyway. Now that it pages with
``WHERE character_id = ? [AND created_at < ?] ORDER BY created_at DESC
LIMIT n``, the single-column index leaves Postgres sorting the whole
per-character set to hand back fifty rows, and each extra page repeats
the sort. A composite ``(character_id, created_at DESC)`` matches both
the filter and the ordering, so a page is an index range scan.

``DESC`` is written explicitly rather than left to Postgres' ability to
read an ascending index backwards: it costs nothing, and it documents
which query shape the index exists for.

Index-only, no data change, no column change — safe to roll back.

Revision ID: e7v4p8s10033
Revises: d6u3o0r10032
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e7v4p8s10033"
down_revision = "d6u3o0r10032"
branch_labels = None
depends_on = None


_TABLE = "memory_items"
_INDEX = "ix_memory_items_character_created"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        _TABLE,
        ["character_id", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
