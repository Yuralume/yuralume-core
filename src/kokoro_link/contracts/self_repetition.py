"""Self-repetition extractor port.

Runs every N turns to look at the character's *own* recent replies in
the same conversation and produce a short natural-language hint about
what they've been overusing — repeated topics, repeated phrasings,
repeated openings, recurring metaphors. The hint is then re-injected
into the chat prompt next turn as semantic anti-repetition framing
("you've been doing X — don't do X again").

Why a separate aux processor instead of relying on the chat LLM's own
attention:

- Last-N message context buries the assistant's lines among user
  turns; without explicit framing the model treats its own past
  replies as ambient context rather than commitments it has to vary.
- A keyword list catches a fixed vocabulary but misses every novel
  pattern the model wanders into. Semantic detection over the
  assistant's own recent prose handles both — same lever fusion's
  critic uses for short stories, scaled down for chat.
- Running it every turn would double per-turn LLM cost. Running every
  N turns + caching the hint amortises the cost across N turns at
  the price of slightly stale guidance, which is fine because
  patterns form gradually and don't change after one reply.

Hints are intentionally short, single-paragraph prose (not structured
findings) so the chat prompt can inline them as one anti-repetition
rail without a parser. Empty / whitespace results mean "nothing
worth flagging" and the rail is skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message


class SelfRepetitionExtractorPort(Protocol):
    async def extract(
        self,
        *,
        character: Character,
        recent_assistant_messages: Sequence[Message],
    ) -> str:
        """Return a short anti-repetition hint, or empty string when
        the LLM judges there's nothing worth flagging.

        Implementations should fail-soft: any exception or unparseable
        output → return empty string so the caller can simply skip the
        injection rail without bringing the chat path down.
        """
