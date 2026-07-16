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


class TTSVoiceCatalogPort(Protocol):
    async def list_voices(self) -> list[TTSVoice]:
        """Return product-facing voice options."""
