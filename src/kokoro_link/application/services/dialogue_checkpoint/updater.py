"""The post-turn machine that advances a pair's dialogue checkpoint.

Runs on the background post-turn (D3), never on the request path. The
old design put a summarisation call *in front of the player's reply*,
every turn; this one runs behind it, on a small fraction of turns, and
the prompt reads whatever the last successful run left behind. A
checkpoint lagging a few turns costs nothing, because everything it has
not yet absorbed is still in the prompt as raw text.

Six decisions, in the order they are taken:

1. **Is a merge earned?** Two independent conditions, either of which
   is enough. Weight: the backlog estimates at or above
   ``backlog_trigger_tokens``. Pressure: the backlog has grown to within
   a small margin of everything the loaded window can physically hold
   (:func:`window_pressure_threshold`). The second condition is not a
   refinement of the first — without it a trigger set above what a full
   middle band weighs is simply unreachable, and no checkpoint is ever
   written at all.
2. **What may be absorbed?** Everything newer than the current coverage
   and *older than the raw tail* — never the tail itself (D5).
3. **Did the merge produce anything?** An empty return keeps the
   previous checkpoint (D4: last-good, never a fallback that widens the
   prompt back out to raw turns).
4. **Did it actually compress?** The inflation guard: the new summary
   must estimate smaller than the old summary plus the backlog it
   swallowed. A merge that grew is not a compression, it is a
   transcript, and it is refused.
5. **Do the messages it merged still exist?** The merge is an LLM call
   several seconds long, and the player can reverse a turn while it
   runs. Re-read before writing; if anything the merge absorbed has
   since been deleted, throw the merge away. Prose cannot be un-merged.
6. **Is the row still as it was read?** A compare-and-swap on the
   cursor *and* the stale flag. Of two replicas merging the same
   backlog, the loser drops its work — the winner absorbed the same
   messages, so nothing is lost.

   The stale half of that predicate is also what closes the gap
   question 5 cannot: the re-read and the write are two statements, and
   an undo landing *between* them passes both. So the undo guard raises
   the stale latch whenever a merge could be in flight over the rows it
   is about to delete, and this CAS — which read ``stale=False`` — then
   loses. Whichever order the two actually interleave, either this write
   never lands or it lands and is immediately marked for rebuild; there
   is no ordering in which a deleted turn stays folded into the summary.

**What a declining run costs.** Two reads — the checkpoint row and the
recent-message window — on every post-turn where the feature is on. That
is the price of asking question 1, and it is paid in the background,
after the player already has their reply. The window read is the same
query the chat prompt makes, so it is warm. Deployments with the flag
off construct no updater at all and pay nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

from kokoro_link.application.services.dialogue_checkpoint.window import (
    split_window,
    total_tokens,
    window_pressure_threshold,
)
from kokoro_link.application.services.dialogue_window_loader import (
    UnifiedMessageWindow,
    load_unified_recent_window,
)
from kokoro_link.contracts.dialogue_checkpoint import (
    DialogueCheckpointMergerPort,
    DialogueCheckpointRepositoryPort,
)
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message, MessageKind
from kokoro_link.domain.entities.dialogue_checkpoint import (
    DialogueCheckpoint,
    checkpoint_cursor_key,
)
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.llm_output.tokens import estimate_tokens

_LOGGER = logging.getLogger(__name__)

STUCK_STREAK_WARN_AT = 3
"""Consecutive non-writing merge attempts before the log turns urgent.

A refused or empty merge leaves the backlog exactly where it was, so the
next run retries the same material with a little more of it. One is
ordinary. A run of them is not: the boundary is frozen while the window
keeps sliding, and the messages caught between the two are on their way
out of the prompt without ever having been summarised. That condition is
silent by construction — nothing the player sees changes, and every
individual outcome is one the machine reports on ordinary turns too — so
it has to be counted or it cannot be noticed at all.
"""


class CheckpointUpdateOutcome(str, Enum):
    """Why an update run ended where it did. Logged, and asserted on in
    tests — the machine has no other observable behaviour when it
    declines to write."""

    DISABLED = "disabled"
    """The flag is off, or a dependency is unwired."""

    BACKLOG_TOO_SMALL = "backlog_too_small"
    """Under the trigger. The normal outcome of most turns."""

    NOTHING_TO_MERGE = "nothing_to_merge"
    """Backlog is all tool-only artifacts, or the tolerance filter left
    nothing summarisable behind."""

    MERGE_EMPTY = "merge_empty"
    """The merger returned nothing — provider failure, fake model, or a
    genuinely contentless backlog. Last-good is kept."""

    INFLATED = "inflated"
    """The merge grew instead of compressing. Refused; the backlog stays
    uncovered and the next run tries again with more to work from."""

    BACKLOG_CHANGED = "backlog_changed"
    """Something this merge absorbed no longer exists.

    The player reversed a turn while the merge was in flight. The
    summary in hand describes messages that have been deleted, and prose
    has no un-merge, so it is discarded unwritten. The next post-turn
    starts over from what is actually there."""

    LOST_RACE = "lost_race"
    """Another replica moved the cursor first, or the checkpoint was
    marked stale while this merge ran. Either way the row is no longer
    the row this work was computed against, so the work is dropped —
    deliberately."""

    COVERAGE_VIOLATION = "coverage_violation"
    """The computed boundary was not strictly older than the raw tail.
    Structurally impossible; refused rather than trusted."""

    WRITTEN = "written"


@dataclass(frozen=True, slots=True)
class CheckpointUpdateReport:
    outcome: CheckpointUpdateOutcome
    backlog_tokens: int = 0
    absorbed_messages: int = 0
    summary_tokens: int = 0
    backlog_messages: int = 0
    """Summarisable rows in the backlog when the trigger was evaluated."""

    window_pressure: bool = False
    """The run was triggered by the window filling up rather than by the
    backlog's weight. Observability only — nothing branches on it — but
    it is the difference between "this deployment's trigger is tuned"
    and "this deployment's trigger is unreachable and the geometric
    backstop is doing all the work"."""

    stuck_streak: int = 0
    """Consecutive runs for this pair that attempted a merge and moved
    the boundary nowhere, this one included. Zero on any other outcome
    (see :data:`STUCK_STREAK_WARN_AT` and ``_STUCK_OUTCOMES``)."""


class DialogueCheckpointUpdater:
    """Advances one pair's checkpoint.

    Stateless as far as *correctness* goes — every decision is taken
    from what the two repositories say on this run, and a fresh instance
    behaves identically to one that has run a thousand times. The single
    piece of retained state is the per-pair stuck counter below, which
    is read by nothing but a log line.
    """

    def __init__(
        self,
        *,
        checkpoints: DialogueCheckpointRepositoryPort,
        merger: DialogueCheckpointMergerPort,
        conversations: ConversationRepositoryPort,
        window_messages: int,
        raw_tail_limit: int,
        backlog_trigger_tokens: int,
        enabled: bool = True,
    ) -> None:
        self._checkpoints = checkpoints
        self._merger = merger
        self._conversations = conversations
        self._window_messages = max(1, int(window_messages))
        self._raw_tail_limit = max(1, int(raw_tail_limit))
        self._backlog_trigger_tokens = max(1, int(backlog_trigger_tokens))
        self._enabled = enabled
        # Observability only, and self-limiting: an entry exists only
        # while a pair is *currently* failing to write, and is deleted
        # the moment one succeeds or stops trying. Per-process by
        # nature — two replicas each count their own attempts, which is
        # what each of them can actually attest to.
        self._stuck_streaks: dict[tuple[str, str], int] = {}

    async def run(
        self,
        *,
        character: Character,
        operator_id: str,
        now: datetime,
    ) -> CheckpointUpdateReport:
        """Advance the checkpoint if the backlog has earned a merge.

        Never raises. This runs inside the fail-soft post-turn, where a
        raised exception would take the subsystems after it down with
        it — and a checkpoint that failed to advance costs nothing more
        than an unusually long raw section on the next prompt.
        """
        if not self._enabled or not operator_id:
            return CheckpointUpdateReport(CheckpointUpdateOutcome.DISABLED)
        try:
            report = await self._run(
                character=character, operator_id=operator_id, now=now,
            )
        except Exception:
            _LOGGER.exception(
                "dialogue checkpoint update crashed character=%s",
                character.id,
            )
            return CheckpointUpdateReport(CheckpointUpdateOutcome.DISABLED)
        return self._account_for_streak(
            report, character=character, operator_id=operator_id,
        )

    def _account_for_streak(
        self,
        report: CheckpointUpdateReport,
        *,
        character: Character,
        operator_id: str,
    ) -> CheckpointUpdateReport:
        """Count consecutive stuck runs, and shout once it is a pattern.

        What counts is :data:`_STUCK_OUTCOMES` — the runs that leave the
        boundary frozen *while they were willing to move it*: the
        backlog was big enough, the merge was attempted, and nothing
        landed. Every other outcome either moved the boundary or never
        intended to, and both of those reset the count rather than
        merely not incrementing it: a pair that recovers must not carry
        an old streak into a future stumble and cross the threshold on
        the first one.
        """
        pair = (character.id, operator_id)
        if report.outcome not in _STUCK_OUTCOMES:
            self._stuck_streaks.pop(pair, None)
            return report
        streak = self._stuck_streaks.get(pair, 0) + 1
        self._stuck_streaks[pair] = streak
        if streak >= STUCK_STREAK_WARN_AT:
            _LOGGER.warning(
                "dialogue checkpoint has not advanced for %d consecutive "
                "attempts (last outcome=%s) character=%s operator=%s "
                "backlog_messages=%d backlog_tokens=%d. The coverage "
                "boundary is frozen while the loaded window keeps sliding, "
                "so messages between the two are leaving the prompt "
                "unsummarised.",
                streak,
                report.outcome.value,
                character.id,
                operator_id,
                report.backlog_messages,
                report.backlog_tokens,
            )
        return replace(report, stuck_streak=streak)

    async def _run(
        self,
        *,
        character: Character,
        operator_id: str,
        now: datetime,
    ) -> CheckpointUpdateReport:
        checkpoint = await self._checkpoints.get(
            character_id=character.id, operator_id=operator_id,
        )
        loaded = await self._load_window(character.id)
        messages = loaded.messages
        # A stale checkpoint is rebuilt from scratch, and "from scratch"
        # has to include the *split*, not only the previous summary. Its
        # cursor is the boundary of a summary that is being thrown away;
        # splitting against it would put everything older than that
        # cursor into ``window.covered``, where the rebuild never reads
        # it and the prompt never renders it — the old summary's whole
        # reach deleted rather than re-derived. Splitting against
        # ``None`` re-merges every row the window still holds.
        rebuild = checkpoint is not None and checkpoint.stale
        window = split_window(
            list(messages),
            checkpoint=None if rebuild else checkpoint,
            raw_tail_limit=self._raw_tail_limit,
        )
        backlog = _summarisable(window.middle)
        backlog_tokens = total_tokens(backlog)
        # Either condition earns the call. See ``window_pressure_threshold``
        # for why the weight test alone is not merely conservative but
        # can be unsatisfiable.
        pressure_threshold = window_pressure_threshold(
            window_messages=len(messages),
            raw_tail_limit=self._raw_tail_limit,
        )
        pressure = loaded.saturated and len(backlog) >= pressure_threshold
        if backlog_tokens < self._backlog_trigger_tokens and not pressure:
            return CheckpointUpdateReport(
                CheckpointUpdateOutcome.BACKLOG_TOO_SMALL,
                backlog_tokens=backlog_tokens,
                backlog_messages=len(backlog),
            )
        if pressure and backlog_tokens < self._backlog_trigger_tokens:
            _LOGGER.info(
                "dialogue checkpoint merging on window pressure rather than "
                "backlog weight (%d rows >= %d, only %d est. tokens against "
                "a %d trigger) character=%s operator=%s. A deployment where "
                "this is the usual reason has its trigger set above what a "
                "full window can weigh.",
                len(backlog), pressure_threshold, backlog_tokens,
                self._backlog_trigger_tokens, character.id, operator_id,
            )
        # Frontier-safe unconditionally — not "safe for the tolerance of
        # whichever turn happened to trigger this run". One checkpoint
        # per pair is read back on *every* later turn, and text sanitised
        # for a community turn would be a restricted original re-entering
        # a frontier prompt days later through a door nobody watches.
        #
        # The cost is real and accepted: on a community-tolerance
        # conversation the pre-DH3 summariser did keep restricted text in
        # its input, so material that used to survive into the older-turn
        # summary is now represented only by its safe replacement — or,
        # where there is none, not at all. Narrowing in the one direction
        # a shared artifact can safely take.
        safe_backlog = sanitize_messages_for_tolerance(
            list(backlog), content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        if not safe_backlog:
            return CheckpointUpdateReport(
                CheckpointUpdateOutcome.NOTHING_TO_MERGE,
                backlog_tokens=backlog_tokens,
                backlog_messages=len(backlog),
                window_pressure=pressure,
            )
        previous_summary = (
            "" if checkpoint is None or rebuild else checkpoint.summary_text
        )
        if rebuild:
            _LOGGER.info(
                "dialogue checkpoint rebuilding from scratch (stale) "
                "character=%s operator=%s: re-summarising all %d window "
                "row(s) behind the raw tail, not merging onto the old "
                "summary. Everything older than the window is not "
                "recoverable — a rebuild is a reset to what is still "
                "loadable.",
                character.id, operator_id, len(backlog),
            )
        result = await self._merger.merge(
            character=character,
            previous_summary=previous_summary,
            messages=list(safe_backlog),
        )
        merged = result.summary
        if not merged:
            _LOGGER.info(
                "dialogue checkpoint merge produced nothing; keeping "
                "last-good character=%s operator=%s backlog_tokens=%d",
                character.id, operator_id, backlog_tokens,
            )
            return CheckpointUpdateReport(
                CheckpointUpdateOutcome.MERGE_EMPTY,
                backlog_tokens=backlog_tokens,
                backlog_messages=len(backlog),
                window_pressure=pressure,
            )
        merged_tokens = estimate_tokens(merged)
        ceiling = estimate_tokens(previous_summary) + total_tokens(
            safe_backlog,
        )
        if merged_tokens >= ceiling:
            _LOGGER.warning(
                "dialogue checkpoint merge inflated (%d >= %d est. tokens); "
                "keeping the previous checkpoint character=%s operator=%s",
                merged_tokens, ceiling, character.id, operator_id,
            )
            return CheckpointUpdateReport(
                CheckpointUpdateOutcome.INFLATED,
                backlog_tokens=backlog_tokens,
                backlog_messages=len(backlog),
                summary_tokens=merged_tokens,
                window_pressure=pressure,
            )
        candidate = DialogueCheckpoint.create(
            character_id=character.id,
            operator_id=operator_id,
            summary_text=merged,
            boundary=backlog[-1],
            now=now,
            model=result.model,
        )
        if not _respects_raw_tail(candidate, window.raw_tail):
            _LOGGER.error(
                "dialogue checkpoint coverage would reach the raw tail — "
                "refusing (character=%s operator=%s). The D5 invariant "
                "says this cannot happen; something upstream reordered "
                "the window or shrank the tail.",
                character.id, operator_id,
            )
            return CheckpointUpdateReport(
                CheckpointUpdateOutcome.COVERAGE_VIOLATION,
                backlog_tokens=backlog_tokens,
                backlog_messages=len(backlog),
                window_pressure=pressure,
            )
        if not await self._backlog_still_exists(character.id, backlog):
            _LOGGER.warning(
                "dialogue checkpoint merge absorbed message(s) that have "
                "since been deleted — discarding it unwritten (character=%s "
                "operator=%s). The next post-turn re-merges what is actually "
                "there.",
                character.id, operator_id,
            )
            return CheckpointUpdateReport(
                CheckpointUpdateOutcome.BACKLOG_CHANGED,
                backlog_tokens=backlog_tokens,
                backlog_messages=len(backlog),
                summary_tokens=merged_tokens,
                window_pressure=pressure,
            )
        landed = await self._checkpoints.save(
            candidate,
            expected_message_key=(
                None if checkpoint is None
                else checkpoint.covers_until_message_key
            ),
            expected_stale=checkpoint is not None and checkpoint.stale,
        )
        if not landed:
            _LOGGER.info(
                "dialogue checkpoint CAS lost; dropping this merge "
                "character=%s operator=%s",
                character.id, operator_id,
            )
            return CheckpointUpdateReport(
                CheckpointUpdateOutcome.LOST_RACE,
                backlog_tokens=backlog_tokens,
                backlog_messages=len(backlog),
                summary_tokens=merged_tokens,
                window_pressure=pressure,
            )
        return CheckpointUpdateReport(
            CheckpointUpdateOutcome.WRITTEN,
            backlog_tokens=backlog_tokens,
            backlog_messages=len(backlog),
            absorbed_messages=len(backlog),
            summary_tokens=merged_tokens,
            window_pressure=pressure,
        )

    async def _load_window(self, character_id: str) -> UnifiedMessageWindow:
        """The same window, loaded the same way, as the prompt reads.

        Through ``dialogue_window_loader``, which collapses cross-channel
        mirror copies. That is not a nicety: the reader splits a
        deduplicated list and this side used to split the raw one, so on
        any pair with a messaging channel bound the two sides disagreed
        about how many rows there were — and therefore about where the
        raw tail began. D5 ("the checkpoint never covers a message the
        player can still undo") was being checked here against a
        boundary the prompt did not share. The duplicated rows were also
        being weighed twice against the trigger and the inflation
        ceiling.

        The whole :class:`UnifiedMessageWindow` rather than its list,
        because the collapse is also what makes the *count* ambiguous:
        "fewer rows than the limit" means "mirrors were collapsed and
        rows are still falling off the back" on one pair and "that is
        the entire history" on another, and only the first of those is
        window pressure.
        """
        return await load_unified_recent_window(
            self._conversations,
            character_id=character_id,
            limit=self._window_messages,
        )

    async def _backlog_still_exists(
        self, character_id: str, backlog: tuple[Message, ...],
    ) -> bool:
        """Did everything this merge absorbed survive the merge?

        The merge is an LLM call lasting seconds, and it runs on the
        background post-turn while the player is free to reverse their
        last turn. The undo guard's *coverage* test cannot help here: it
        checks the checkpoint as stored, and the checkpoint as stored
        does not cover the reversed turn yet — the summary that will
        cover it is still in this process's memory. So that test passes,
        undo proceeds, and this run then writes a summary of messages
        that no longer exist. There is no un-merge; the reversed turn
        would be in the character's memory forever.

        This check catches every undo that finishes before the re-read.
        The ones that land in the gap between this re-read and the write
        are caught by the stale latch instead — the guard raises it
        whenever an in-flight merge could have reached the rows it is
        deleting, and the CAS below refuses a write whose read said
        otherwise. Two mechanisms because there are two windows, and
        neither one covers the other's.

        The check is a re-read, and the comparison is by cursor key
        (content fingerprint) because a domain ``Message`` has no id.

        **Absence is only evidence when the message could still be
        seen.** The window holds the newest N rows, so anything that
        scrolled off the back during the merge is legitimately missing
        and means nothing. Only a message *newer than the oldest row
        still in the window* — inside the range the re-read can actually
        attest to — counts as deleted. Without that qualifier a single
        new turn arriving during the merge would push the oldest backlog
        row out of view and every merge on an active conversation would
        be discarded as if the player had undone something.

        An empty re-read is treated as changed: it says nothing is there
        at all, which cannot be reconciled with a backlog that was.
        """
        if not backlog:
            return True
        current = (await self._load_window(character_id)).messages
        if not current:
            return False
        horizon = min(_as_utc(message.created_at) for message in current)
        present = {checkpoint_cursor_key(message) for message in current}
        return not any(
            _as_utc(message.created_at) >= horizon
            and checkpoint_cursor_key(message) not in present
            for message in backlog
        )


_STUCK_OUTCOMES = frozenset({
    CheckpointUpdateOutcome.INFLATED,
    CheckpointUpdateOutcome.MERGE_EMPTY,
    CheckpointUpdateOutcome.BACKLOG_CHANGED,
})
"""Outcomes where a merge was attempted, nothing landed, and no other
writer advanced the boundary either.

``LOST_RACE`` is deliberately absent: the boundary *did* move, by the
winner, over the same backlog. Nothing is stuck and nothing is lost.
"""


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


def _summarisable(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """Drop what a summary cannot use.

    Same filter the pre-DH3 summariser applied: ``TOOL_ONLY`` rows carry
    artifact URLs and empty text, and blank content contributes nothing
    but a role label.
    """
    return tuple(
        message for message in messages
        if message.kind is not MessageKind.TOOL_ONLY
        and (message.content or "").strip()
    )


def _respects_raw_tail(
    candidate: DialogueCheckpoint, raw_tail: tuple[Message, ...],
) -> bool:
    """D5, stated as the invariant itself rather than as a proxy for it.

    "The checkpoint never covers a message in the raw tail" is exactly
    what ``covers`` answers, so the check asks it directly instead of
    comparing timestamps and re-deriving the tie-break rule a second
    time — two implementations of the same boundary is how they drift.

    The boundary comes from the middle band, which ``split_window`` built
    by removing the tail, so this is structurally true already. It is
    still checked: being wrong means a turn the player can still undo
    gets folded into a summary nothing can un-merge, and being right
    costs three comparisons.

    An empty tail is a violation, not a pass — it would mean the split
    produced a middle with nothing newer than it.
    """
    if not raw_tail:
        return False
    return not any(candidate.covers(message) for message in raw_tail)


__all__ = [
    "STUCK_STREAK_WARN_AT",
    "CheckpointUpdateOutcome",
    "CheckpointUpdateReport",
    "DialogueCheckpointUpdater",
]
