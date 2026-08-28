"""Splitting a loaded message window against a checkpoint's coverage.

Pure functions over an already-loaded, already-deduplicated list. They
do no I/O, know nothing about the flag, and are shared by the two sides
that must agree on where the boundaries are — the reader that builds the
prompt and the updater that decides what to merge next. A disagreement
between those two is how a message gets dropped from the prompt without
ever having been summarised, so they are one implementation.

Three regions, oldest to newest:

``covered``
    already inside ``checkpoint.summary_text``. Never rendered raw.
``middle``
    newer than the checkpoint, older than the raw tail. Rendered raw
    while it fits the budget, and the next thing the updater merges.
``raw tail``
    the last few turns, verbatim, always. Exempt from the budget: a
    ceiling that could eat the tail would let a long reply shorten the
    context of the reply after it.

The module also owns the *other* geometric fact the two sides must
agree on — how much middle band the window can physically hold before
the oldest of it scrolls out of the load limit entirely. See
:func:`window_pressure_threshold`.
"""

from __future__ import annotations

from dataclasses import dataclass

from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint
from kokoro_link.llm_output.tokens import estimate_tokens

PROMPT_RAW_TAIL_MESSAGES = 3
"""How many of the newest messages are always rendered verbatim.

The canonical statement of the raw tail's width. Three consumers need
the same number and they are in three different layers, which is why it
lives in the pure geometry module rather than in any one of them:

* the **prompt** renders that many turns raw
  (``chat_service._PROMPT_RAW_RECENT_MESSAGE_LIMIT`` is this constant);
* the **updater** refuses to absorb them (D5); and
* the **undo guard** has to know how far back a merge that is in flight
  right now could possibly have reached — a question that is answered in
  rows of tail and nothing else (see
  ``turn_undo/steps/dialogue_checkpoint.py``).

The container passes it to the reader and the updater explicitly, so
those two read one value at runtime rather than one constant each.
"""

WINDOW_PRESSURE_SAFETY_MARGIN = 3
"""How many middle-band slots are kept in hand by
:func:`window_pressure_threshold`.

The margin buys the *next* post-turn a chance to run before anything
falls off the back of the window. A merge that fires exactly when the
middle band is full is already too late: the run can decline (an empty
merge, a refused inflation, a lost CAS), and the turn after it the
oldest middle message is outside the loaded window — neither in the
summary nor in the prompt, and no later run can ever reach it again.

Three is one full turn plus a spare row: enough for one declined run to
be retried, small enough that it does not pull the trigger forward on
ordinary conversations, where the token trigger fires first anyway.
"""


def window_pressure_threshold(
    *,
    window_messages: int,
    raw_tail_limit: int,
    safety_margin: int = WINDOW_PRESSURE_SAFETY_MARGIN,
) -> int:
    """Middle-band message count at which a merge stops being optional.

    ``window_messages`` is **the number of rows the caller actually has
    in hand, after mirror collapse** — not the row limit it asked the
    repository for. The two are different numbers on any pair with a
    messaging channel bound, and using the configured limit here made
    this backstop unreachable on exactly those pairs: the loader asks for
    30 rows, collapses the fan-out mirrors among them, and *deliberately
    does not top the window back up*, so a window of 30 requested rows
    routinely holds 24. A threshold computed from 30 (24) against a
    middle band that now tops out at 21 is a condition that can never
    become true — the same unreachable-trigger shape this function was
    written to remove, one layer up.

    The token trigger asks "is this backlog worth an LLM call?". It is
    the right question and it is the wrong *only* question, because it
    is denominated in tokens while the thing that loses data is
    denominated in messages: the window holds ``window_messages`` rows,
    ``raw_tail_limit`` of them are the tail, so the middle band can
    never exceed ``window_messages - raw_tail_limit`` — and a backlog
    that can never grow past that ceiling can never reach a token
    trigger set above what that many rows weigh.

    That is not hypothetical. At the shipped defaults (window 30, tail
    3, trigger 1500 estimated tokens) the middle band tops out at 27
    rows, and 27 rows of ordinary Traditional-Chinese chat estimate at
    roughly 500-1100 tokens. The trigger was unreachable: no checkpoint
    would ever have been written on a real conversation.

    So the trigger is two conditions, not one, and this is the second:
    *the window is nearly full, therefore merge now regardless of
    weight*. It keeps the coverage boundary chasing the window rather
    than being outrun by it, which is the invariant the whole design
    rests on — everything the checkpoint has not absorbed has to still
    be visible as raw text, and the only place raw text lives is inside
    the loaded window.

    Never returns less than 1: a degenerate configuration (tail as wide
    as the window) should merge as soon as there is anything at all to
    merge, not never.
    """
    capacity = int(window_messages) - int(raw_tail_limit)
    return max(1, capacity - int(safety_margin))


@dataclass(frozen=True, slots=True)
class DialogueWindow:
    """One loaded window, split against a checkpoint's coverage."""

    covered: tuple[Message, ...]
    middle: tuple[Message, ...]
    raw_tail: tuple[Message, ...]

    @property
    def uncovered(self) -> tuple[Message, ...]:
        """Everything the checkpoint has not absorbed, oldest first."""
        return self.middle + self.raw_tail


def split_window(
    messages: list[Message],
    *,
    checkpoint: DialogueCheckpoint | None,
    raw_tail_limit: int,
) -> DialogueWindow:
    """Cut ``messages`` into covered / middle / raw tail.

    ``checkpoint`` of ``None`` — no checkpoint yet — leaves ``covered``
    empty, which is what makes the very first turn and the millionth
    turn take the same code path.

    Coverage is decided per message rather than by finding an index,
    because the window is a *merge* of several conversations: two
    threads' messages interleave by timestamp, and a message older than
    the boundary can appear after a newer one only if the repository's
    ordering broke. Asking each message keeps the split correct either
    way, and keeps a covered straggler out of the prompt instead of
    letting it slip in behind the boundary.
    """
    if raw_tail_limit <= 0:
        raise ValueError("raw_tail_limit must be positive")
    if not messages:
        return DialogueWindow(covered=(), middle=(), raw_tail=())
    if checkpoint is None or not checkpoint.summary_text:
        uncovered = list(messages)
        covered: list[Message] = []
    else:
        covered = [m for m in messages if checkpoint.covers(m)]
        uncovered = [m for m in messages if not checkpoint.covers(m)]
    if len(uncovered) <= raw_tail_limit:
        return DialogueWindow(
            covered=tuple(covered),
            middle=(),
            raw_tail=tuple(uncovered),
        )
    return DialogueWindow(
        covered=tuple(covered),
        middle=tuple(uncovered[:-raw_tail_limit]),
        raw_tail=tuple(uncovered[-raw_tail_limit:]),
    )


def message_tokens(message: Message) -> int:
    """Estimated cost of one transcript line.

    The role label the renderer prefixes is not modelled — it is two
    CJK characters against a line of dozens, well inside the estimator's
    own error bar, and modelling it here would couple this module to a
    prompt-section detail it should not know.
    """
    return estimate_tokens(message.content or "")


def total_tokens(messages: tuple[Message, ...] | list[Message]) -> int:
    return sum(message_tokens(message) for message in messages)


def fit_to_budget(
    middle: tuple[Message, ...],
    *,
    raw_tail: tuple[Message, ...],
    budget_tokens: int,
) -> tuple[Message, ...]:
    """Trim the middle band, oldest first, until the raw text fits.

    The tail is counted against the budget but never trimmed, so a tail
    that is on its own over budget yields an empty middle rather than a
    truncated tail. That is the intended direction: the newest turns are
    the ones the reply is actually answering, and the material the trim
    drops is the material the checkpoint is about to absorb anyway.
    """
    if budget_tokens <= 0:
        return ()
    remaining = budget_tokens - total_tokens(raw_tail)
    if remaining <= 0:
        return ()
    kept: list[Message] = []
    # Newest-first so the *oldest* middle messages are the ones dropped.
    for message in reversed(middle):
        cost = message_tokens(message)
        if cost > remaining:
            # ``break``, not ``continue``: what survives has to be a
            # contiguous run ending at the tail. Skipping one long turn
            # to fit two shorter, older ones would leave a hole in the
            # transcript, and a conversation read across a hole is worse
            # than a shorter one — the model has no way to know a reply
            # is answering something it cannot see.
            break
        remaining -= cost
        kept.append(message)
    kept.reverse()
    return tuple(kept)


__all__ = [
    "PROMPT_RAW_TAIL_MESSAGES",
    "WINDOW_PRESSURE_SAFETY_MARGIN",
    "DialogueWindow",
    "fit_to_budget",
    "message_tokens",
    "split_window",
    "total_tokens",
    "window_pressure_threshold",
]
