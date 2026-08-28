"""Pending-follow-up repository port.

Storage-agnostic CRUD for the deferred-reply queue. In-memory adapter is
used by unit tests; the SQLAlchemy adapter persists in the
``pending_follow_ups`` table.

Lookups the service / dispatcher need:

* ``find_open_for_conversation(...)`` / ``list_open_for_conversation(...)``
  — the newest open row, or all of them. A conversation can hold several
  (one busy-defer plus any number of scheduled promises), so anything
  that must not miss one takes the list.
* ``list_due(...)`` — the proactive-scheduler tick walks rows whose
  ``scheduled_for <= now`` and ``status == queued`` (or ``resolving``
  left dangling by an earlier crash), grouped by character so the
  dispatcher can apply per-character gating.

Cascade helpers mirror the journal / arc / schedule shape so character
deletion stays atomic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.pending_follow_up import PendingFollowUp


class PendingFollowUpRepositoryPort(Protocol):
    async def add(self, follow_up: PendingFollowUp) -> None:
        """Persist a new row."""

    async def save(self, follow_up: PendingFollowUp) -> None:
        """Upsert. Used when status / messages mutate."""

    async def get(self, follow_up_id: str) -> PendingFollowUp | None:
        """Fetch a single row by id."""

    async def coalesce_promise_intent(
        self,
        follow_up_id: str,
        *,
        expected_intent: str,
        new_intent: str,
        now: datetime,
    ) -> bool:
        """Compare-and-swap ``promise_intent``. ``True`` iff it landed.

        The one write on this port that is **not** a whole-row upsert, and
        it exists because its caller cannot use one. The HV4 chat audit
        (F5) merges a newly-caught claim into a repair row it wrote
        minutes ago: read the intent, add a line, write it back — the
        read-modify-write shape that cost PF three rounds of repair. Two
        audits racing (one failing capability, a player asking twice)
        would both read the same intent and the later ``save`` would
        erase the earlier one's claim, which is precisely the lie-goes-
        unowned outcome the feature exists to prevent.

        So the update is conditional, and the condition is the field
        itself: ``expected_intent`` must still be what the caller read.
        Whoever writes second finds the predicate false, is told so, and
        opens its own row instead — occasionally two repair rows, never a
        dropped claim.

        Two further predicates come along because they are the same
        atomicity question asked about the row's *lifecycle*, and asking
        them in Python before this call would only re-open the window
        they close:

        * ``status`` must still be ``queued``. A row already ``resolving``
          is being composed right now, from a copy read before this merge;
          the dispatcher will write that copy back when it finishes.
        * ``scheduled_for`` must still be in the future. A row that is
          already due may have been handed to a release worker in this
          very tick, and that worker's blind row-level ``save`` would
          overwrite this merge with no way to notice.

        ``updated_at`` is advanced to ``now`` so the row's own staleness
        sweeps see the touch. Nothing else on the row is written.
        """

    async def find_open_for_conversation(
        self, conversation_id: str,
    ) -> PendingFollowUp | None:
        """Return the **newest** open (``queued`` / ``resolving``) row for
        the conversation, of any ``kind``, or ``None``.

        A conversation can hold more than one open row at a time, and the
        docstring that used to claim otherwise ("only one row per
        conversation may be open") was wrong: the merge policy collapses
        *busy-defer* rows into one, but ``_persist_message_promises``
        appends a ``SCHEDULED_PROMISE`` row per extracted promise without
        ever looking for an existing one. A conversation with an open
        busy-defer row and a newer promise row is ordinary.

        So this is a "give me one" convenience, not a census — and the
        one it gives is not necessarily the one any particular caller
        means. ``ChatService`` cancels the open **busy-defer** row via its
        own kind-filtered lookup, which is a different row from the one
        this returns. Anything that has to reason about *all* open rows —
        turn-undo's pre-turn snapshot above all — must use
        ``list_open_for_conversation``.
        """

    async def list_open_for_conversation(
        self, conversation_id: str,
    ) -> list[PendingFollowUp]:
        """Every open (``queued`` / ``resolving``) row of the conversation,
        any ``kind``, oldest-first.

        Exists because ``find_open_for_conversation`` answers a different
        question than turn-undo asks. The pre-turn journal snapshots what
        undo must be able to put back, and the row a turn cancels is the
        open *busy-defer* one — which ``find_open_for_conversation``
        silently loses the moment a newer scheduled-promise row exists,
        because it sorts by ``queued_at`` and takes one. Undo would then
        restore the promise (unchanged, so a no-op) while the cancelled
        busy-defer row stayed cancelled for ever: the player's awaited
        reply gone, which is the exact failure the snapshot exists to
        prevent.
        """

    async def list_due(
        self,
        *,
        now: datetime,
        limit: int = 50,
    ) -> list[PendingFollowUp]:
        """Return queued rows whose ``scheduled_for <= now``.

        Ordered by ``scheduled_for`` ascending (FIFO) and capped at
        ``limit`` so a backlog can't starve other tick work. The
        dispatcher applies per-character busy / energy filtering before
        actually firing — this port is a coarse cursor.
        """

    async def list_stale_resolving(
        self,
        *,
        now: datetime,
        older_than_seconds: float,
        limit: int = 50,
    ) -> list[PendingFollowUp]:
        """Return ``resolving`` rows last touched more than
        ``older_than_seconds`` ago (``updated_at < now - older_than_seconds``).

        A ``resolving`` row is a release a worker claimed but never finished:
        normally the worker flips it to ``resolved`` / back to ``queued`` within
        seconds, so a row still ``resolving`` well past that age is a crashed
        worker's orphan that ``list_due`` (``queued`` only) never rescues. The
        **distributed** release reconciler sweeps these so the row is re-enqueued
        and driven to a terminal state; the at-most-once visible-slot claim on the
        release path keeps a rescue from re-sending an already-delivered message.

        Ordered oldest-first, capped at ``limit``. Distributed-only — the embedded
        tick's ``list_due`` is deliberately left untouched (byte-identical
        self-host behaviour)."""

    async def list_open_for_character(
        self, character_id: str,
    ) -> list[PendingFollowUp]:
        """All open rows belonging to ``character_id``. Used by tests
        and by the cascading delete flow."""

    async def list_created_since(
        self, conversation_id: str, since: datetime,
    ) -> list[PendingFollowUp]:
        """Rows of ``conversation_id`` queued at or after ``since``.

        The boundary is inclusive because a turn stamps the journal's
        ``turn_started_at`` and the row it defers from the *same* clock
        read — an exclusive floor would miss exactly the row turn-undo
        is looking for.

        Status is deliberately not filtered: a row the turn created and
        then closed again is still the turn's to take back. Ordered
        oldest-first.

        **This is a fallback, not the primary anchor.** A window cannot
        tell a row the turn wrote from a row that merely *landed* while
        the turn was open, and the scheduled-promise writer runs in the
        background post-turn — so the previous turn's promise routinely
        lands inside the next turn's window. Callers must ignore any row
        that carries a ``turn_record_id``; those are claimed by
        ``list_created_by_turn`` and by nothing else.
        """

    async def list_created_by_turn(
        self, conversation_id: str, turn_record_id: str,
    ) -> list[PendingFollowUp]:
        """Rows of ``conversation_id`` stamped with ``turn_record_id``.

        The exact answer to "did this turn write this row", available
        whenever the writer had a turn in hand. Unlike the time window it
        does not care when the write actually landed, which is the whole
        point: the only writer that stamps an anchor is the background
        post-turn, whose clock the turn does not control.

        Scoped by conversation as well as by anchor. The anchor alone
        would do — a turn belongs to one conversation — but undo's
        contract is that it never touches another thread's rows, and a
        predicate is cheaper than trusting that invariant to hold for
        ever. Ordered oldest-first.
        """

    async def delete(self, follow_up_id: str) -> bool:
        """Delete one row. ``True`` iff a row was actually removed.

        A hard delete, not a ``cancelled`` flip: the only caller is
        turn-undo, whose claim is that the turn never happened, and a
        cancelled row would keep telling the dispatcher — and the audit
        trail — about a deferred reply that was never owed.
        """

    async def delete_for_conversation(self, conversation_id: str) -> int:
        """Cascade-delete every row tied to a conversation."""

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade-delete every row belonging to ``character_id``.

        Called from ``CharacterService.delete_character`` so deferred
        replies don't outlive their owner.
        """
