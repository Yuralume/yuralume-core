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
    """Records the ``character_id`` each call was scoped to (EC5-C).

    Mimics the real filtering boundary at the level the route controls:
    the general voice is always present, the exclusive one only for the
    one character id the caller opts into. The real filtering happens at
    the cloud TTS service (keyed off the origin header the adapter sends)
    — this stub is a stand-in for "whatever context-aware catalog the
    route is talking to", not a reimplementation of that service.
    """

    def __init__(
        self,
        voices: list[TTSVoice],
        *,
        exclusive_voice: TTSVoice | None = None,
        exclusive_for_character_id: str | None = None,
    ) -> None:
        self._voices = voices
        self._exclusive_voice = exclusive_voice
        self._exclusive_for_character_id = exclusive_for_character_id
        self.calls: list[str | None] = []

    async def list_voices(
        self, *, character_id: str | None = None,
    ) -> list[TTSVoice]:
        self.calls.append(character_id)
        voices = list(self._voices)
        if (
            self._exclusive_voice is not None
            and character_id is not None
            and character_id == self._exclusive_for_character_id
        ):
            voices.append(self._exclusive_voice)
        return voices


class _UnavailableCatalog:
    async def list_voices(
        self, *, character_id: str | None = None,
    ) -> list[TTSVoice]:
        raise TTSUnavailable("down")


class _CharacterRepository:
    """Minimal ``CharacterRepositoryPort.get`` stand-in for ownership checks."""

    def __init__(self, characters: dict[str, SimpleNamespace]) -> None:
        self._characters = characters

    async def get(self, character_id: str) -> SimpleNamespace | None:
        return self._characters.get(character_id)


def _character(character_id: str, *, user_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=character_id, user_id=user_id)


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
        current_user_id="default",
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
        current_user_id="default",
    )

    assert response.enabled is False
    assert response.voice_presets == []


# --- EC5-C: exclusive-voice filtering pin tests -----------------------------


def _general_voice() -> TTSVoice:
    return TTSVoice(id="marin", label="Marin", prompt_lang="ja")


def _exclusive_voice() -> TTSVoice:
    return TTSVoice(id="yumi-voice", label="Yumi (official)", prompt_lang="ja")


@pytest.mark.asyncio
async def test_tts_assets_omits_exclusive_voice_with_no_character_context() -> None:
    """Ticket item 1: default listing (no ``character_id``) never shows an
    exclusive voice — the pre-EC5-C behaviour for the general picker."""
    catalog = _Catalog(
        [_general_voice()],
        exclusive_voice=_exclusive_voice(),
        exclusive_for_character_id="char-managed",
    )

    response = await list_tts_assets(
        container=SimpleNamespace(tts_voice_catalog=catalog),
        current_user_id="alice",
    )

    voice_ids = {v.voice_id for v in response.voice_presets}
    assert voice_ids == {"marin"}
    assert catalog.calls == [None]


@pytest.mark.asyncio
async def test_tts_assets_shows_exclusive_voice_for_its_bound_managed_character() -> None:
    """Pin test: a request naming the character the exclusive voice is
    actually bound to sees it in the listing."""
    catalog = _Catalog(
        [_general_voice()],
        exclusive_voice=_exclusive_voice(),
        exclusive_for_character_id="char-managed",
    )
    repo = _CharacterRepository({
        "char-managed": _character("char-managed", user_id="alice"),
    })

    response = await list_tts_assets(
        character_id="char-managed",
        container=SimpleNamespace(
            tts_voice_catalog=catalog,
            character_repository=repo,
        ),
        current_user_id="alice",
    )

    voice_ids = {v.voice_id for v in response.voice_presets}
    assert voice_ids == {"marin", "yumi-voice"}
    assert catalog.calls == ["char-managed"]


@pytest.mark.asyncio
async def test_tts_assets_hides_exclusive_voice_for_an_ordinary_character() -> None:
    """Pin test: an ordinary (non-managed) character's own id never
    unlocks a voice bound to a different, managed character."""
    catalog = _Catalog(
        [_general_voice()],
        exclusive_voice=_exclusive_voice(),
        exclusive_for_character_id="char-managed",
    )
    repo = _CharacterRepository({
        "char-ordinary": _character("char-ordinary", user_id="alice"),
    })

    response = await list_tts_assets(
        character_id="char-ordinary",
        container=SimpleNamespace(
            tts_voice_catalog=catalog,
            character_repository=repo,
        ),
        current_user_id="alice",
    )

    voice_ids = {v.voice_id for v in response.voice_presets}
    assert voice_ids == {"marin"}
    assert catalog.calls == ["char-ordinary"]


@pytest.mark.asyncio
async def test_tts_assets_ignores_character_id_owned_by_another_user() -> None:
    """A foreign character id must not leak whether it is managed — the
    request degrades to the same result as omitting the parameter."""
    catalog = _Catalog(
        [_general_voice()],
        exclusive_voice=_exclusive_voice(),
        exclusive_for_character_id="char-managed",
    )
    repo = _CharacterRepository({
        "char-managed": _character("char-managed", user_id="bob"),
    })

    response = await list_tts_assets(
        character_id="char-managed",
        container=SimpleNamespace(
            tts_voice_catalog=catalog,
            character_repository=repo,
        ),
        current_user_id="alice",
    )

    voice_ids = {v.voice_id for v in response.voice_presets}
    assert voice_ids == {"marin"}
    assert catalog.calls == [None]


@pytest.mark.asyncio
async def test_tts_assets_ignores_unknown_character_id() -> None:
    """A stale/deleted character id degrades gracefully instead of 404ing
    the whole picker."""
    catalog = _Catalog([_general_voice()])
    repo = _CharacterRepository({})

    response = await list_tts_assets(
        character_id="does-not-exist",
        container=SimpleNamespace(
            tts_voice_catalog=catalog,
            character_repository=repo,
        ),
        current_user_id="alice",
    )

    assert {v.voice_id for v in response.voice_presets} == {"marin"}
    assert catalog.calls == [None]


@pytest.mark.asyncio
async def test_tts_assets_self_host_zero_regression_no_character_repository() -> None:
    """Self-host containers that never wire ``character_repository`` (many
    per-route unit tests, and any deployment without one) must behave
    exactly as before EC5-C: passing a ``character_id`` degrades to no
    context instead of erroring."""
    catalog = _Catalog([_general_voice()])

    response = await list_tts_assets(
        character_id="char-1",
        container=SimpleNamespace(tts_voice_catalog=catalog),
        current_user_id="default",
    )

    assert {v.voice_id for v in response.voice_presets} == {"marin"}
    assert catalog.calls == [None]


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
