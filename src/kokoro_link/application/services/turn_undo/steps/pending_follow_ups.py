"""TU4 — put deferred replies back the way the turn found them.

A turn can move the deferred-reply queue in three ways, and each one has
its own way of hurting the player if undo ignores it:

1. **Created a row.** A busy-defer turn writes one, and post-turn promise
   extraction writes another. Reverting only the messages leaves the row
   behind, so when its time comes the character earnestly answers a
   message that no longer exists.
2. **Merged into a row.** If a row was already open, the turn appends to
   it instead of creating one. Deleting that row would throw away
   messages from *earlier* turns; the correct reversal is to write the
   pre-turn row back.
3. **Cancelled a row.** A normal reply cancels the open defer row. Undo
   that ignores this direction makes the reply the player was waiting
   for vanish permanently — which is the failure a safety net exists to
   prevent, not to cause.

Cases 2 and 3 are one operation: write ``journal.prev_open_follow_ups``
back. That is why the journal snapshots the *open rows at turn start*
rather than "the row this turn cancelled" — one pre-turn fact answers
both, and the merge case produces no cancellation event to hang a record
on. Case 1 is its complement: a row queued since the turn started that
the snapshot does not name is a row the turn created.

**Case 2 has a second writer, and it is not the busy-defer path.** HV4's
chat honesty audit coalesces a caught claim into a repair row the
conversation already owes (F5), and that row is anchored on the *earlier*
turn that opened it. So this turn's claim lives inside a row the anchor
pass will not touch — deleting it would throw away the earlier turn's
still-visible lie — and rewinding it is entirely the snapshot restore's
job, exactly as for a merged defer row. The pairing that makes it work
is the merge's own precondition: the auditor only merges into a row that
predates the turn, which is the same thing as "a row this turn's
snapshot names". Pinned end-to-end in ``test_turn_undo_follow_ups``'s
``test_undo_rewinds_a_repair_coalesced_into_an_earlier_turns_row``.

**Which rows the turn created is answered by anchor first, window
second.** A ``scheduled_promise`` row is written by the *background*
post-turn, so its ``queued_at`` is the instant that write landed, not
the instant of the turn that promised it. Time-window-only, the sequence
below deleted a live promise:

    10:00:00  turn A replies "I'll remind you at 22:00"; post-turn queued
    10:00:04  player sends turn B; its journal snapshots an empty queue
    10:00:08  turn A's post-turn writes promise row P
    10:00:12  player undoes turn B

P is inside B's window and absent from B's snapshot, so B's undo read it
as "a row this turn created" and hard-deleted it. Turn A is still on
screen, the character's promise is still in the transcript, and because
the delete is hard rather than a cancel the release reconciler cannot
bring it back either. Every row that carries an anchor is therefore
claimed by that anchor alone; the window only ever looks at anchorless
rows.

Anchorless is not a legacy-only state. A busy-defer row never gets one:
that branch runs no post-turn, mints no turn record, and its journal
carries ``turn_record_id = None`` — but it also writes its row *inline*
during the turn, which is the one case the window has always been exact
for. So the two passes partition the work rather than overlapping:
anchored rows by id, anchorless rows by window.

The window keeps running even when the journal has an anchor, and that
is deliberate: during a rolling deployment the same turn can have been
served by a build that had no anchor to stamp. Those rows are exactly
the ones the window still finds, and leaving them would leak every
promise written in the deploy window. It degrades to the old behaviour
for old rows and to the exact behaviour for new ones.

The opposite race — undo landing *before* the turn's own post-turn
writes — is not this step's to close and cannot be: there is nothing in
the table yet to delete. The TU2 tombstone closes it instead, at the
gate ``_do_post_turn`` checks after its extraction returns and before it
persists anything, so the promise is never written at all.

Two things this step deliberately does not do:

* **Re-enqueue a release job for a restored row.** The row's original job
  is still queued (cancelling a row never withdrew one), and where it is
  not, the distributed reconciler re-enqueues any still-due row on its
  next sweep. Minting one here would need the coordinator lease that undo
  has no business holding.
* **Touch rows in other conversations.** Everything is scoped to
  ``journal.conversation_id``; a promise the same character owes on
  another thread is not this turn's doing.
"""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.application.services.pending_follow_up_release import (
    PendingFollowUpReleaseWithdrawer,
)
from kokoro_link.application.services.turn_journal_snapshots import (
    follow_up_from_dict,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)
from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.domain.entities.pending_follow_up import PendingFollowUp

_LOGGER = logging.getLogger(__name__)


class _PreTurnRows:
    """The snapshot read twice over: as ids, and as entities.

    They are kept apart on purpose. The ids come from the raw payload and
    are therefore always available; the entities come from the codec, and
    a single malformed one is skipped. If one list served both jobs, a
    snapshot entry that failed to decode would stop naming its row — and
    the delete pass would then read that pre-existing row as something
    the turn created and remove it. Losing a restore is a disappointment;
    deleting a row the turn never made is a bug.
    """

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.ids: set[str] = set()
        self.rows: list[PendingFollowUp] = []
        for payload in payloads:
            row_id = payload.get("id")
            if row_id:
                self.ids.add(str(row_id))
            try:
                self.rows.append(follow_up_from_dict(payload))
            except Exception:
                _LOGGER.exception(
                    "Undo: undecodable pending follow-up snapshot id=%s",
                    row_id,
                )


class PendingFollowUpRestoreStep(UndoStep):
    """Delete what the turn queued; put back what it cancelled or merged."""

    name = "pending-follow-ups"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.pending_follow_ups
        if repository is None:
            return
        snapshot = _PreTurnRows(context.journal.prev_open_follow_ups)
        await self._delete_rows_the_turn_queued(
            context, repository, snapshot, tally,
        )
        await self._restore_pre_turn_rows(repository, snapshot, tally)

    async def _delete_rows_the_turn_queued(
        self,
        context: UndoContext,
        repository: PendingFollowUpRepositoryPort,
        snapshot: _PreTurnRows,
        tally: UndoTally,
    ) -> None:
        withdrawer = self._withdrawer(context)
        for row in await self._rows_this_turn_queued(context, repository):
            if row.id in snapshot.ids:
                # Queued at the same instant the turn began, but the
                # snapshot names it — so it predates the turn.
                continue
            if not await repository.delete(row.id):
                # Someone else got there first. The row is gone either
                # way, which is all this step wanted.
                continue
            tally.deleted_follow_ups += 1
            if withdrawer is not None:
                # Row first, job second: a job that outlives its row is
                # skipped by the handler, whereas a row that outlives its
                # job waits for the reconciler to notice it.
                tally.cancelled_follow_up_jobs += await withdrawer.withdraw(
                    row, now=context.now,
                )

    @staticmethod
    async def _rows_this_turn_queued(
        context: UndoContext,
        repository: PendingFollowUpRepositoryPort,
    ) -> list[PendingFollowUp]:
        """The turn's rows: anchored ones by id, anchorless ones by window.

        Keyed into a dict rather than concatenated because a row can
        legitimately answer both passes (its own turn's anchor, its own
        turn's window) and deleting it twice would double the tally and
        withdraw its release job twice. Insertion order is preserved, so
        the delete pass still walks oldest-first within each pass.
        """
        journal = context.journal
        rows: dict[str, PendingFollowUp] = {}
        if journal.turn_record_id:
            for row in await repository.list_created_by_turn(
                journal.conversation_id, journal.turn_record_id,
            ):
                rows[row.id] = row
        for row in await repository.list_created_since(
            journal.conversation_id, journal.turn_started_at,
        ):
            if row.turn_record_id is not None:
                # Anchored, and the anchor did not name this turn — so
                # the row landed during this turn's window without
                # belonging to it. That is the background post-turn of
                # some *other* turn, and its promise is still owed.
                continue
            rows[row.id] = row
        return list(rows.values())

    async def _restore_pre_turn_rows(
        self,
        repository: PendingFollowUpRepositoryPort,
        snapshot: _PreTurnRows,
        tally: UndoTally,
    ) -> None:
        for row in snapshot.rows:
            current = await repository.get(row.id)
            if current == row:
                # The turn left it alone. Writing it back would be
                # harmless but would report a restore that never
                # happened — and most turns land here.
                continue
            await repository.save(row)
            tally.restored_follow_ups += 1

    @staticmethod
    def _withdrawer(
        context: UndoContext,
    ) -> PendingFollowUpReleaseWithdrawer | None:
        queue = context.deps.follow_up_release_queue
        if queue is None:
            # Self-host: releases run from the in-process scheduler tick,
            # so there is no queued job to take back. Absent, not broken.
            return None
        return PendingFollowUpReleaseWithdrawer(queue=queue)


__all__ = ["PendingFollowUpRestoreStep"]
