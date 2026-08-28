"""The prompt-side read of a pair's dialogue checkpoint.

Answers one question — *given this window of messages, what does the
dialogue section of the prompt contain?* — and answers it as
``(messages, summary)``, the pair ``_prepare_prompt_dialogue_context``
has always returned. Keeping that shape is what lets the flag be a
genuine switch: the prompt builder, the section registry and the
goldens see the same two values whichever side of it they came from.

Failure directions, all of them narrowing (D4):

* **no checkpoint yet** — the caller degrades to the pre-DH3 path. That
  is the reader returning ``None`` rather than inventing something.
* **repository unreachable** — same. A checkpoint that cannot be read
  is indistinguishable from one that does not exist.
* **checkpoint marked stale** — same again. A summary known to contain
  something that is no longer true is worth less than no summary: the
  usual cause is a turn the player reversed, and the prompt is where
  that turn would come back to life.
* **budget exceeded** — the middle band is trimmed oldest-first. The
  raw tail is never trimmed.

There is no direction in which a failure here makes the prompt *bigger*,
which is the whole correction DH3 makes to the old summariser: that one
fell back to the complete raw message list, so an unstable provider
lengthened the prompt exactly when it was least able to handle it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kokoro_link.application.services.dialogue_checkpoint.window import (
    fit_to_budget,
    split_window,
)
from kokoro_link.contracts.dialogue_checkpoint import (
    DialogueCheckpointRepositoryPort,
)
from kokoro_link.domain.entities.conversation import Message

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DialoguePromptContext:
    """What the dialogue section of one prompt is made of."""

    messages: list[Message]
    """Raw transcript lines, oldest first: the surviving middle band
    followed by the whole raw tail."""

    summary: str
    """The checkpoint text, rendered where the old per-turn summary was.
    Empty when there is no checkpoint."""

    dropped_middle: int = 0
    """Middle-band messages the token budget removed. Observability
    only — nothing branches on it."""


class DialogueCheckpointReader:
    def __init__(
        self,
        *,
        checkpoints: DialogueCheckpointRepositoryPort,
        raw_tail_limit: int,
        prompt_budget_tokens: int,
    ) -> None:
        self._checkpoints = checkpoints
        self._raw_tail_limit = max(1, int(raw_tail_limit))
        self._prompt_budget_tokens = max(1, int(prompt_budget_tokens))

    async def read(
        self,
        *,
        character_id: str,
        operator_id: str,
        recent_messages: list[Message],
    ) -> DialoguePromptContext | None:
        """Build the dialogue section, or ``None`` to use the old path.

        ``None`` means "there is no checkpoint to read" — no row yet, no
        operator to key on, a store that would not answer, **or a row
        marked stale**. The caller then runs the pre-DH3 behaviour
        unchanged, which is the correct degradation: without a summary
        of the older turns, showing them raw is the only way not to lose
        them.

        Stale counts as absent because of what the flag *means*: some
        of what this summary asserts is no longer true — most often a
        turn the player took back, which is the one thing a player is
        entitled to expect the character to forget. The flag is raised
        by an undo and cleared by the next rebuild, and between those
        two there can be many turns; a reader that kept using the row
        would have the character repeating a deleted secret for all of
        them. Dropping it costs context and keeps a promise, which is
        the only direction this trade can go.
        """
        if not operator_id:
            return None
        try:
            checkpoint = await self._checkpoints.get(
                character_id=character_id, operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "dialogue checkpoint read failed character=%s", character_id,
            )
            return None
        if (
            checkpoint is None
            or checkpoint.stale
            or not checkpoint.summary_text
        ):
            return None
        window = split_window(
            recent_messages,
            checkpoint=checkpoint,
            raw_tail_limit=self._raw_tail_limit,
        )
        kept_middle = fit_to_budget(
            window.middle,
            raw_tail=window.raw_tail,
            budget_tokens=self._prompt_budget_tokens,
        )
        return DialoguePromptContext(
            messages=list(kept_middle + window.raw_tail),
            summary=checkpoint.summary_text,
            dropped_middle=len(window.middle) - len(kept_middle),
        )


__all__ = ["DialogueCheckpointReader", "DialoguePromptContext"]
