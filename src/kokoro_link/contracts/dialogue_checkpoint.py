"""Ports for the cumulative dialogue checkpoint (DH3).

Two protocols, deliberately separate from
:mod:`kokoro_link.contracts.dialogue_summarizer`.

``DialogueSummarizerPort`` is *not* extended or reused. Its own docstring
names schedule / arc / proactive as consumers: it condenses a window of
turns into a blurb for a planner, it is stateless, and it restates. The
merge port below does the opposite job — it folds a new window into an
existing summary and has to decide what stops being true. Sharing one
protocol would mean one prompt trying to satisfy both contracts, and the
first tuning change for either would silently move the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint


@dataclass(frozen=True, slots=True)
class DialogueCheckpointMergeResult:
    """One merge attempt: the new summary, and what produced it.

    The model id rides back with the text rather than being read off the
    merger afterwards. An adapter that resolves its model per call (the
    active-model preference can change between turns) would otherwise
    have to keep the last one in a field, and one field shared by
    concurrent merges for different characters is a label that reports
    the wrong model under exactly the load where the audit matters.

    ``summary`` empty means the merge did not happen — an empty backlog,
    a fake model, a provider failure. ``model`` is then meaningless and
    the caller ignores it.
    """

    summary: str
    model: str = ""

    @classmethod
    def failed(cls) -> "DialogueCheckpointMergeResult":
        return cls(summary="")


class DialogueCheckpointRepositoryPort(Protocol):
    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> DialogueCheckpoint | None:
        """The pair's current checkpoint, or ``None`` before the first."""

    async def save(
        self,
        checkpoint: DialogueCheckpoint,
        *,
        expected_message_key: str | None,
        expected_stale: bool = False,
    ) -> bool:
        """Compare-and-swap the pair's checkpoint. True when it landed.

        The predicate is *the whole state the caller read*, not just the
        cursor:

        ``expected_message_key``
            the ``covers_until_message_key`` the caller read before it
            computed ``checkpoint``; ``None`` claims there was no row at
            all. Two replicas merging the same backlog concurrently
            therefore produce exactly one winner, and the loser drops its
            work. Dropping it is correct rather than merely acceptable:
            the winner absorbed the same backlog, so nothing is lost, and
            the loser's summary was computed against a cursor that no
            longer exists.
        ``expected_stale``
            the ``stale`` flag the caller read. This is what makes
            :meth:`mark_stale` a real latch. ``mark_stale`` deliberately
            does *not* move the cursor — it has no new coverage to
            claim — so a cursor-only predicate would be satisfied by a
            merge that started before the latch was set and would clear
            the flag on its way past, folding the very material the latch
            was raised about into the summary permanently.

            It is ``expected_stale`` rather than a hard ``stale = false``
            because the run that clears the latch legitimately reads a
            stale row: it rebuilds from scratch, and writing a fresh
            non-stale checkpoint is the whole point. What must not land
            is a write from a caller that read a *different* staleness
            than the row now has.

        Returning ``False`` is an ordinary outcome, not an error. An
        implementation raises only for a genuinely broken store.
        """

    async def mark_stale(
        self, *, character_id: str, operator_id: str, now: datetime,
    ) -> bool:
        """Flag the pair's checkpoint for a from-scratch rebuild.

        Unconditional — no CAS. It is a one-way latch towards the safer
        state, and the writer that could otherwise clear it out from
        under the latch is excluded by ``save``'s ``expected_stale``
        predicate rather than by anything here. False when there is no
        row.
        """

    async def delete_for_character(self, character_id: str) -> int:
        """Drop every checkpoint for a character. Returns rows removed."""


class DialogueCheckpointMergerPort(Protocol):
    async def merge(
        self,
        *,
        character: Character,
        previous_summary: str,
        messages: list[Message],
    ) -> DialogueCheckpointMergeResult:
        """Fold ``messages`` into ``previous_summary`` and return the whole.

        The returned summary **replaces** the previous one; it is not an
        addendum. An empty ``previous_summary`` is the first-ever build.

        An empty ``summary`` means the merge could not be done — an empty
        backlog, a fake model, a provider failure. The caller keeps the
        previous checkpoint on empty and never treats it as "the summary
        is now nothing" (D4: last-good, never a wider fallback). An
        implementation therefore never raises for a provider error; it
        returns :meth:`DialogueCheckpointMergeResult.failed`.
        """


__all__ = [
    "DialogueCheckpointMergeResult",
    "DialogueCheckpointMergerPort",
    "DialogueCheckpointRepositoryPort",
]
