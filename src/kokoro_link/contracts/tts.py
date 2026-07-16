"""Port for character text-to-speech.

Spike-stage shape — one synth call per request, returns raw audio
bytes. The adapter knows nothing about caching, persistence, or URLs;
that lives one layer up in :class:`TTSService` so different backends
    (OpenAI TTS, custom voice servers, wrapped local engines) can slot
    in by writing only the HTTP / inference part.

Adapters MUST raise :class:`TTSUnavailable` when the backend is not
reachable / not configured, and :class:`TTSError` on any other
failure. The route layer translates the former to a 503 (greys out
the play button) and the latter to a 502 (something's wrong, retry
might help).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class TTSUnavailable(Exception):
    """Backend is intentionally not configured (no base_url, no
    voice config) — the route returns 503 so the UI can fall back to
    "TTS disabled" instead of surfacing a noisy error."""


class TTSError(Exception):
    """Backend was reached but the synth call failed (network blip,
    server-side OOM, model error)."""


@dataclass(frozen=True, slots=True)
class TTSWeights:
    """Legacy per-character weight pins kept for old adapters."""

    gpt_weights_path: str = ""
    sovits_weights_path: str = ""


@dataclass(frozen=True, slots=True)
class TTSRequest:
    """Single synth request payload.

    ``voice_id`` is the current cross-provider contract. Legacy local
    path fields remain for old rows and adapters.
    """

    text: str
    voice_id: str = ""
    """Stable voice id understood by the external TTS capability service.

    Legacy GPT-SoVITS fields below are kept for old adapters/tests, but new
    providers should use ``voice_id`` and ignore local path details.
    """
    ref_audio_path: str = ""
    prompt_text: str = ""
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    text_split_method: str = "cut5"
    top_k: int = 5
    top_p: float = 1.0
    temperature: float = 1.0
    speed_factor: float = 1.0
    weights: TTSWeights = TTSWeights()
    """Legacy per-request weight pin for old local adapters."""
    character_id: str = ""


@dataclass(frozen=True, slots=True)
class TTSResult:
    """Synth output."""

    audio: bytes
    media_type: str = "audio/wav"
    """MIME type for the route's response headers / file extension
    decision. Adapters tag provider-specific output accordingly."""


class TTSPort(ABC):
    """Synthesise one chunk of text into audio bytes."""

    @abstractmethod
    async def synthesize(self, request: TTSRequest) -> TTSResult: ...
