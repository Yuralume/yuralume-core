"""Background TTS pregeneration controlled by scoped preferences."""

from __future__ import annotations

import logging
import re
from typing import Protocol

from kokoro_link.contracts.repositories import PreferencesRepositoryPort
from kokoro_link.application.services.scoped_preferences import (
    get_preference_with_user_fallback,
    set_user_preference,
)
from kokoro_link.domain.entities.conversation import MessageContentMode

_LOGGER = logging.getLogger(__name__)

TTS_PREGENERATION_PREFERENCE_KEY = "tts_pregeneration"

_ACTION_PATTERN = re.compile(r"\*[^*\n]+\*")


class TTSSynthesizer(Protocol):
    @property
    def enabled(self) -> bool:
        """Whether synthesis can run for at least one character."""

    async def synthesize(self, *, character_id: str, text: str) -> object:
        """Synthesize and cache a character line."""


class TTSPregenerationService:
    """Runs the same TTS cache path before the user clicks play.

    The chat flow schedules :meth:`pregenerate_if_enabled` as a
    fire-and-forget task after the assistant message is persisted. This
    service owns the preference read and all TTS failures so chat
    latency and delivery are never affected by audio generation.
    """

    def __init__(
        self,
        *,
        tts_service: TTSSynthesizer | None,
        preferences: PreferencesRepositoryPort,
    ) -> None:
        self._tts_service = tts_service
        self._preferences = preferences

    async def is_enabled(self, *, user_id: str | None = None) -> bool:
        raw = await get_preference_with_user_fallback(
            self._preferences,
            TTS_PREGENERATION_PREFERENCE_KEY,
            user_id=user_id,
        )
        if isinstance(raw, dict):
            return bool(raw.get("enabled", False))
        if isinstance(raw, bool):
            return raw
        return False

    async def set_enabled(
        self,
        enabled: bool,
        *,
        user_id: str | None = None,
    ) -> bool:
        if user_id:
            await set_user_preference(
                self._preferences,
                TTS_PREGENERATION_PREFERENCE_KEY,
                {"enabled": bool(enabled)},
                user_id=user_id,
            )
        else:
            await self._preferences.set(
                TTS_PREGENERATION_PREFERENCE_KEY,
                {"enabled": bool(enabled)},
            )
        return bool(enabled)

    async def pregenerate_if_enabled(
        self,
        *,
        character_id: str,
        text: str,
        user_id: str | None = None,
        content_mode: MessageContentMode | str = MessageContentMode.NORMAL,
    ) -> None:
        tts = self._tts_service
        if tts is None or not tts.enabled:
            return
        if _is_nsfw_content_mode(content_mode):
            return
        try:
            enabled = await self.is_enabled(user_id=user_id)
        except Exception:
            _LOGGER.exception("tts pregeneration: preference read failed")
            return
        if not enabled:
            return

        speech = speech_text_for_tts(text)
        if not speech:
            return
        try:
            await tts.synthesize(character_id=character_id, text=speech)
        except Exception:
            _LOGGER.exception(
                "tts pregeneration failed character=%s",
                character_id,
            )


def speech_text_for_tts(text: str) -> str:
    """Mirror ChatBubble's speech extraction for cache compatibility."""
    raw = text or ""
    if not raw:
        return ""
    if "*" not in raw:
        return raw.strip()
    return re.sub(r"\s+", " ", _ACTION_PATTERN.sub(" ", raw)).strip()


def _is_nsfw_content_mode(value: MessageContentMode | str) -> bool:
    if isinstance(value, MessageContentMode):
        return value is MessageContentMode.NSFW
    return str(value or "").strip().lower() == MessageContentMode.NSFW.value
