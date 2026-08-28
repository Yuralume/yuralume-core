"""undone_turns — the tombstone that stops a reversed turn coming back (TU1).

A turn's post-turn extraction runs after the reply is delivered: a
fire-and-forget task in embedded mode, a queued worker job in hosted. An
undo that lands while that work is still in flight loses — the extraction
writes memories, emotion events, promises and a state suggestion for a
turn whose deletes have already run, and the turn resurrects one
subsystem at a time. This table is the interlock the post-turn checks
before it writes anything.

One table, three columns, no backfill:

- ``turn_record_id`` — ``VARCHAR(36)`` primary key. The gate's only
  lookup key, because an id is all a background post-turn carries.
  Making it the PK also makes recording idempotent: undoing the same
  turn twice is one row, not a duplicate-key error.
- ``conversation_id`` — ``VARCHAR(36)``, FK to ``conversations`` with
  ``ON DELETE CASCADE``, indexed. Cleanup and diagnosability; never part
  of the gate's question.
- ``undone_at`` — ``TIMESTAMPTZ``, indexed. The GC sweep's only
  predicate. Unindexed it would full-scan a table that only grows
  between sweeps.

**Why a separate table rather than a flag on ``turn_journals``:** the
last thing an undo does is delete its journal row. A marker stored there
would die with the record it exists to outlive. And the post-turn asks
by ``turn_record_id``, which inside the journal's single ``payload_json``
blob is not a queryable thing at all.

**No ``character_id`` column, on purpose.** Nothing reads a tombstone by
character, and adding the column would drag an operational marker into
the character backup/erasure boundary registry — whose completeness scan
keys on exactly that column name — for no read that wants it.

Revision ID: s9g5a2d10044
Revises: r8f4z1c10043
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s9g5a2d10044"
down_revision: Union[str, None] = "r8f4z1c10043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "undone_turns"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("turn_record_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_undone_turns_conversation_id", _TABLE, ["conversation_id"],
    )
    op.create_index("ix_undone_turns_undone_at", _TABLE, ["undone_at"])


def downgrade() -> None:
    op.drop_index("ix_undone_turns_undone_at", table_name=_TABLE)
    op.drop_index("ix_undone_turns_conversation_id", table_name=_TABLE)
    op.drop_table(_TABLE)
