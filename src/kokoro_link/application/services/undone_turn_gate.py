"""The undo tombstone protocol: written in one place, asked in one place.

A turn's post-turn extraction (memories, emotion events, promises, the
state suggestion) runs *after* the reply reaches the player — a
fire-and-forget task in embedded mode, a claimed worker job in hosted.
An undo can therefore land while that work is still in flight, and
nothing in the rollback can stop it: the deletes run, then the
extraction writes, and the turn comes back from the dead one subsystem
at a time. Waiting for the in-flight task is unreachable in hosted,
where the extraction is in a different process; a durable row is not.

Both halves of that interlock live here rather than one in the undo and
one in :class:`~kokoro_link.application.services.chat_service.ChatService`,
because the two halves are one agreement. The retention window is the
clearest example: the writer's GC and the reader's guarantee are the
same number, and a number that lives in two files is a number that will
eventually be two numbers.

**One reader, not two.** The embedded background task and the hosted
worker job both funnel into ``ChatService._do_post_turn``, and that body
is the only place the gate is asked. Asking it again at the worker entry
would save a few reads and cost the thing that matters — two gates are
two behaviours, and the one that gets forgotten is the one that matters.

**One reader, two moments.** Inside that single body the question is put
twice, and the second time is the one that catches a live undo. The
first ask is on entry, and it exists to skip the provider call: it can
only ever see an undo that landed before the post-turn started. Between
it and the first write sits the extraction — several seconds of upstream
LLM — and *every* write in the body is on the far side of that gap, so
an undo arriving mid-extraction sails straight past an entry-only check.
On embedded it is worse than a narrow window: the background task's very
first await is that entry ask, reached before the write point has even
persisted the journal, so the turn is not yet undoable and the check is
structurally incapable of seeing a real undo at all. The second ask sits
after the extraction returns with the writes immediately ahead of it,
and reports its own reason (:data:`POST_TURN_SKIPPED_UNDONE_IN_FLIGHT`)
so the two are distinguishable in job outcomes — an operator can see
that this path is really catching turns, not just costing a read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Final

from kokoro_link.contracts.undone_turn import UndoneTurnRepositoryPort
from kokoro_link.domain.entities.undone_turn import UndoneTurn

_LOGGER = logging.getLogger(__name__)


POST_TURN_SKIPPED_UNDONE: Final[str] = "turn_undone"
"""``post_turn_skipped`` reason for a turn already reversed on arrival.

The entry ask's reason: the rollback finished before this post-turn
began, so it is a job holding a turn that no longer exists — most often
a worker retry. Sits alongside ``character_missing`` /
``character_stopped`` / ``conversation_missing`` / ``turn_not_found`` so
the worker's outcome detail names this cause instead of silently
reporting a clean run."""


POST_TURN_SKIPPED_UNDONE_IN_FLIGHT: Final[str] = "turn_undone_in_flight"
"""``post_turn_skipped`` reason for an undo that landed mid-extraction.

Same refusal, different evidence. :data:`POST_TURN_SKIPPED_UNDONE` means
the turn was already reversed when the post-turn began — a retry of a
job whose turn is long gone. This one means the player pressed undo
while the extraction was upstream, which is the race the tombstone was
built for and the only one an entry-only check cannot see. Keeping them
apart is what makes the interlock observable: a deployment where this
reason never appears is one where the second ask is doing nothing, and
that is worth being able to tell."""


UNDONE_TURN_RETENTION: Final[timedelta] = timedelta(days=7)
"""How long a tombstone has to survive.

The floor is the worst case for a hosted post-turn job between enqueue
and its final attempt: five attempts (``DEFAULT_MAX_ATTEMPTS``) with
exponential backoff capped at 300s and a 120s execution lease each, so
well under an hour of queue time — plus however long a worker fleet
outage keeps the queue standing still. Seven days is chosen with a wide
margin on that, because the two sides of the trade are not comparable:
dropping a tombstone early re-opens exactly the race it exists to close,
while keeping it costs one narrow row per undo — and undos are rare
because a player only ever undoes the turn they just regretted."""


class UndoneTurnGate:
    """Records tombstones for reversed turns, and answers the gate.

    Constructed with ``None`` on a deployment where the store is unwired:
    the undo still reverses everything it can, it simply cannot stop a
    post-turn already in flight, which is the behaviour that predates
    the tombstone entirely.
    """

    __slots__ = ("_repository", "_retention")

    def __init__(
        self,
        repository: UndoneTurnRepositoryPort | None = None,
        *,
        retention: timedelta = UNDONE_TURN_RETENTION,
    ) -> None:
        self._repository = repository
        self._retention = retention

    async def is_undone(self, turn_record_id: str | None) -> bool:
        """Has this turn been reversed? The post-turn gate's whole question.

        **Fail-open on error, deliberately.** A missing store, a missing
        anchor or a database hiccup all answer "not undone", so the
        post-turn runs exactly as it did before this interlock existed.
        The alternative — refusing every post-turn while the tombstone
        table is unreachable — would silently stop memory, state and
        promise extraction for every player over a fault that has
        nothing to do with them, to close a race that needs an undo in
        the same few seconds to even occur.
        """
        if self._repository is None or not turn_record_id:
            return False
        try:
            return await self._repository.is_undone(turn_record_id)
        except Exception:
            _LOGGER.exception(
                "undo gate: tombstone lookup failed turn=%s; "
                "letting the post-turn proceed",
                turn_record_id,
            )
            return False

    async def record(
        self,
        *,
        turn_record_id: str | None,
        conversation_id: str,
        now: datetime,
    ) -> bool:
        """Mark a turn as reversed; return whether a tombstone now exists.

        ``turn_record_id`` of ``None`` is a normal outcome rather than a
        caller's mistake: a busy-defer turn runs no post-turn and mints
        no turn record, so there is nothing in flight to gate and nothing
        to write.

        Never raises. The tombstone is a safety net over a race, and
        failing to raise it must not abort the rollback the player
        actually asked for.
        """
        if self._repository is None or not turn_record_id:
            return False
        try:
            await self._repository.record(UndoneTurn.new(
                turn_record_id=turn_record_id,
                conversation_id=conversation_id,
                undone_at=now,
            ))
        except Exception:
            _LOGGER.exception(
                "undo gate: failed to record tombstone turn=%s conversation=%s",
                turn_record_id, conversation_id,
            )
            return False
        await self.prune(now=now)
        return True

    async def prune(self, *, now: datetime) -> int:
        """Drop tombstones no post-turn could still be racing; return the count.

        Called off the back of every :meth:`record`, the same shape the
        journal's own retention uses (pruned at the write that made the
        table grow). It keeps the table bounded by the undo rate inside
        the retention window without introducing a scheduled sweep that
        every deployment would then have to run.

        Best-effort: a failed sweep leaves rows that are merely useless,
        so it must never be the thing that fails an undo.
        """
        if self._repository is None:
            return 0
        try:
            return await self._repository.prune(older_than=now - self._retention)
        except Exception:
            _LOGGER.exception("undo gate: tombstone prune failed")
            return 0


__all__ = [
    "POST_TURN_SKIPPED_UNDONE",
    "POST_TURN_SKIPPED_UNDONE_IN_FLIGHT",
    "UNDONE_TURN_RETENTION",
    "UndoneTurnGate",
]
