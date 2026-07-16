"""Port for pre-TTS text translation.

Phase 1.5 of the voice spike: when the synthesizer voice is native
to a different language than the chat reply (e.g. Kokkoro voice is
Japanese but the LLM replies in Chinese), running the cross-language
inference inside GPT-SoVITS produces noticeable accent / leak
artifacts. Better to translate the reply text first via LLM and let
the TTS adapter run native-language synthesis.

The port stays minimal — one translate call per request, returning
the rendered string. Adapters MUST be fail-soft: any error → return
empty string. The TTSService treats empty as "translation refused,
fall back to original text" so a flaky LLM never bricks the play
button.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TTSTranslatorPort(ABC):
    @abstractmethod
    async def translate(
        self,
        *,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """Render ``text`` in ``target_lang``.

        ``source_lang`` / ``target_lang`` follow the same vocabulary the
        TTS layer uses (``zh``, ``ja``, ``en``...) so callers don't need
        a separate mapping table. Empty return = "skip / failed";
        non-empty = translated body.
        """
