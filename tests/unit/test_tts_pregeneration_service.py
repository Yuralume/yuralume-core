import pytest

from kokoro_link.application.services.tts_pregeneration_service import (
    TTSPregenerationService,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)


class _RecordingTTS:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, *, character_id: str, text: str):
        self.calls.append((character_id, text))
        return object()


@pytest.mark.asyncio
async def test_tts_pregeneration_defaults_to_disabled() -> None:
    prefs = InMemoryPreferencesRepository()
    tts = _RecordingTTS()
    service = TTSPregenerationService(
        tts_service=tts,
        preferences=prefs,
    )

    assert await service.is_enabled() is False

    await service.pregenerate_if_enabled(
        character_id="aiko",
        text="早安",
    )

    assert tts.calls == []


@pytest.mark.asyncio
async def test_tts_pregeneration_runs_when_enabled_and_strips_actions() -> None:
    prefs = InMemoryPreferencesRepository()
    tts = _RecordingTTS()
    service = TTSPregenerationService(
        tts_service=tts,
        preferences=prefs,
    )

    await service.set_enabled(True)
    await service.pregenerate_if_enabled(
        character_id="aiko",
        text="早安 *微笑著揮手* 今天也一起努力吧",
    )

    assert await service.is_enabled() is True
    assert tts.calls == [("aiko", "早安 今天也一起努力吧")]


@pytest.mark.asyncio
async def test_tts_pregeneration_skips_nsfw_content_mode() -> None:
    prefs = InMemoryPreferencesRepository()
    tts = _RecordingTTS()
    service = TTSPregenerationService(
        tts_service=tts,
        preferences=prefs,
    )

    await service.set_enabled(True)
    await service.pregenerate_if_enabled(
        character_id="aiko",
        text="這段不應送 TTS",
        content_mode=MessageContentMode.NSFW,
    )

    assert tts.calls == []


@pytest.mark.asyncio
async def test_tts_pregeneration_user_preference_falls_back_to_global() -> None:
    prefs = InMemoryPreferencesRepository()
    tts = _RecordingTTS()
    service = TTSPregenerationService(
        tts_service=tts,
        preferences=prefs,
    )

    await service.set_enabled(True)
    assert await service.is_enabled(user_id="alice") is True

    await service.set_enabled(False, user_id="alice")

    assert await service.is_enabled(user_id="alice") is False
    assert await service.is_enabled(user_id="bob") is True
