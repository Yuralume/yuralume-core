"""Voice catalog contract for external TTS capability services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TTSVoice:
    id: str
    label: str
    prompt_lang: str = ""
    is_complete: bool = True
    exclusive_card_id: str = ""
    """The official card this voice is licensed to, or ``""`` for a voice
    anyone may pick (EC 系列 D4).

    A partner's character speaks with the voice that was cast for them, and
    that binding lives in the Cloud voice catalog rather than in any
    deployment's config: the install reads it to bind the character at
    birth, and the picker reads it to keep the voice out of everyone else's
    list. Empty on every self-hosted / local catalog, which have no
    exclusive voices to describe."""


class TTSVoiceCatalogPort(Protocol):
    async def list_voices(
        self,
        *,
        character_id: str | None = None,
        character_origin: str = "",
    ) -> list[TTSVoice]:
        """Return product-facing voice options.

        Two optional ways to say "and the voices this card owns", because
        the two callers hold different halves of the same fact:

        * ``character_id`` (EC5-C) — an existing character, whose origin an
          implementation looks up for itself. This is the picker's call.
        * ``character_origin`` (EC5-D) — the official card slug outright,
          for the cloud-exclusive install, which is picking the voice a
          character will be *created* with and so has no character to look
          anything up from. The explicit slug wins if both are given.

        Both default to "no context" and neither may be required: a caller
        with no card in hand — or a self-host/BYOK adapter with no concept
        of exclusive voices at all — calls this exactly as it did before
        either argument existed."""
