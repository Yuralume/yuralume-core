from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from kokoro_link.api.routes.tts import (
    TTSSynthRequest,
    list_tts_assets,
    synthesize_character_tts,
)
from kokoro_link.application.services.nsfw_mode import NsfwModeService
from kokoro_link.contracts.tts import TTSUnavailable
from kokoro_link.contracts.tts_catalog import TTSVoice
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)


class _Catalog:
    def __init__(self, voices: list[TTSVoice]) -> None:
        self._voices = voices

    async def list_voices(self) -> list[TTSVoice]:
        return self._voices


class _UnavailableCatalog:
    async def list_voices(self) -> list[TTSVoice]:
        raise TTSUnavailable("down")


class _RecordingTTS:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, *, character_id: str, text: str):
        self.calls.append((character_id, text))
        return SimpleNamespace(audio_url="/uploads/tts/aiko.wav", cached=False)


@pytest.mark.asyncio
async def test_tts_assets_route_returns_external_voice_catalog() -> None:
    response = await list_tts_assets(
        container=SimpleNamespace(
            tts_voice_catalog=_Catalog(
                [TTSVoice(id="marin", label="Marin", prompt_lang="ja")],
            ),
        ),
    )

    assert response.enabled is True
    assert response.ref_audios == []
    assert response.gpt_weights == []
    assert response.sovits_weights == []
    assert response.voice_presets[0].voice_id == "marin"
    assert response.voice_presets[0].label == "Marin"


@pytest.mark.asyncio
async def test_tts_assets_route_disables_when_catalog_unreachable() -> None:
    response = await list_tts_assets(
        container=SimpleNamespace(tts_voice_catalog=_UnavailableCatalog()),
    )

    assert response.enabled is False
    assert response.voice_presets == []


@pytest.mark.asyncio
async def test_tts_synthesis_route_disabled_while_nsfw_mode_active() -> None:
    prefs = InMemoryPreferencesRepository()
    nsfw = NsfwModeService(preferences=prefs, ttl_seconds=60)
    await nsfw.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id="anime_nsfw",
    )
    await nsfw.enable(user_id="alice")
    tts = _RecordingTTS()

    with pytest.raises(HTTPException) as exc:
        await synthesize_character_tts(
            "char-1",
            TTSSynthRequest(text="不要送出"),
            container=SimpleNamespace(
                tts_service=tts,
                nsfw_mode_service=nsfw,
            ),
            current_user_id="alice",
            _owned_character_id="char-1",
        )

    assert exc.value.status_code == 403
    assert tts.calls == []
