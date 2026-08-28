"""turn_record_id anchors on pending_follow_ups + character_encounter_intents (TU4/TU6).

Undo has to answer one question about every row a turn may have written:
*did this turn write it?* Until now two subsystems answered it with a
time window — "queued at or after ``turn_started_at``" — and both of
their writers run in the **background post-turn**, which is a different
clock from the turn's. The window is therefore wrong in both directions:

* Too wide. Turn A promises "22:00 I'll remind you"; its post-turn is
  still queued when the player sends turn B four seconds later. A's
  promise row lands *after* B's ``turn_started_at``, so undoing B —
  which never promised anything — hard-deletes a promise turn A made in
  a message that is still on screen. The row is deleted, not cancelled,
  so the release reconciler cannot bring it back either.
* Too narrow. Undo the turn before its own post-turn has landed and the
  window finds nothing; the promise is written moments later and stays
  for ever. (The TU2 tombstone is what actually closes this direction —
  see ``undone_turns`` — but only for rows written after the gate.)

``character_encounter_intents`` had a third failure mode on top: its
delete filtered on ``character_id`` and ``created_at`` only, with no
conversation scope at all. One character living in a web conversation
and a LINE conversation at the same time (which ``recent_messages_for_
character`` explicitly supports) meant undoing a turn in one thread
deleted a meeting the *other* thread had just agreed to.

The fix is the same anchor TU3 already uses for emotion events: the
writer stamps the turn's ``turn_records`` id into the row, and undo
deletes by that id. An id is exact whenever it runs, needs no clock
agreement between writer and reader, and cannot reach across turns or
conversations by construction.

Both columns are **nullable, with no backfill**, and that is a load-
bearing choice rather than laziness:

* Rows that predate this migration have no anchor and never will —
  the turn that wrote them is not recoverable from the row. Undo keeps
  its old time-window behaviour for *those* rows only
  (``turn_record_id IS NULL``), so a rolling deployment degrades to the
  status quo instead of silently leaking every in-flight promise.
* The busy-defer row is legitimately anchorless for ever: that branch
  runs no post-turn and therefore mints no turn record, so its journal
  carries ``turn_record_id = None``. It is also written *inline* during
  the turn, which is precisely the case the time window was always
  correct for.

Indexed because the anchor is a lookup key, not a payload: undo's whole
delete pass is one ``WHERE turn_record_id = ?`` per table, and unindexed
that is a full scan of a table whose whole point is to accumulate rows
between releases.

Revision ID: t0h6b3e10045
Revises: s9g5a2d10044
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "t0h6b3e10045"
down_revision: Union[str, None] = "s9g5a2d10044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMN = "turn_record_id"
_TARGETS: tuple[tuple[str, str], ...] = (
    ("pending_follow_ups", "ix_pending_follow_ups_turn_record_id"),
    (
        "character_encounter_intents",
        "ix_character_encounter_intents_turn_record_id",
    ),
)


def upgrade() -> None:
    for table, index_name in _TARGETS:
        op.add_column(
            table,
            sa.Column(_COLUMN, sa.String(length=36), nullable=True),
        )
        op.create_index(index_name, table, [_COLUMN])


def downgrade() -> None:
    for table, index_name in reversed(_TARGETS):
        op.drop_index(index_name, table_name=table)
        op.drop_column(table, _COLUMN)
