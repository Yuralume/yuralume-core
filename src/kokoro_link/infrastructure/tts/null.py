"""No-op TTS adapter — always raises ``TTSUnavailable``.

Used when ``KOKORO_TTS_BASE_URL`` is empty so the rest of the wiring
stays the same shape (service has a port to call) and the route
returns a clean 503 instead of a 500. Mirrors the
``NullFeedComposer`` / ``NullSchedulePlanner`` pattern.
"""

from __future__ import annotations

from kokoro_link.contracts.tts import (
    TTSPort,
    TTSRequest,
    TTSResult,
    TTSUnavailable,
)


class NullTTSAdapter(TTSPort):
    async def synthesize(self, request: TTSRequest) -> TTSResult:
        raise TTSUnavailable("TTS backend is not configured")
