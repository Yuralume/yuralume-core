"""Tests for ``TTSService`` (Yuralume voice spike).

Covers the cache hit / miss split, fail-soft propagation of port
exceptions, and the cache-busting fingerprint when voice config
changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.services.tts_service import TTSService
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessLocked,
)
from kokoro_link.bootstrap.settings import TTSSettings
from kokoro_link.contracts.tts import (
    TTSError,
    TTSPort,
    TTSRequest,
    TTSResult,
    TTSUnavailable,
)
from kokoro_link.contracts.tts_translator import TTSTranslatorPort
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEMO_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _CountingPort(TTSPort):
    def __init__(self, payload: bytes = b"WAV-FAKE") -> None:
        self.calls = 0
        self.last_request: TTSRequest | None = None
        self.last_request_id = ""
        self._payload = payload

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        self.calls += 1
        self.last_request = request
        self.last_request_id = f"tts-test-{self.calls}"
        return TTSResult(audio=self._payload, media_type="audio/wav")


class _FailingPort(TTSPort):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        raise self._exc


class _StaticDemoRuntimeProfileResolver:
    async def resolve_for_operator(self, operator_id: str):
        return DEMO_ACCOUNT_RUNTIME_PROFILE


class _DenySubscriptionGuard:
    async def ensure_character_allowed(self, character) -> None:
        raise SubscriptionAccessLocked("tenant-a")


def _settings(**overrides) -> TTSSettings:
    base = dict(
        base_url="http://localhost:9880",
        ref_audio_path="/data/ref.wav",
        prompt_text="這是參考音檔",
        prompt_lang="zh",
        text_lang="zh",
    )
    base.update(overrides)
    return TTSSettings(**base)


def _storage() -> InMemoryObjectStorage:
    return InMemoryObjectStorage(public_base_url="/uploads")


@pytest.mark.asyncio
async def test_subscription_lock_blocks_tts_before_cache_or_provider(
    tmp_path: Path,
) -> None:
    characters = InMemoryCharacterRepository()
    character = Character.create(
        name="Mio", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    await characters.save(character)
    port = _CountingPort()
    service = TTSService(
        port=port,
        settings=_settings(),
        uploads_dir=tmp_path,
        object_storage=_storage(),
        character_repository=characters,
        subscription_access_guard=_DenySubscriptionGuard(),
    )

    with pytest.raises(SubscriptionAccessLocked):
        await service.synthesize(character_id=character.id, text="嗨")

    assert port.calls == 0


@pytest.mark.asyncio
async def test_synthesize_writes_file_and_returns_url(tmp_path: Path) -> None:
    port = _CountingPort()
    service = TTSService(
        port=port, settings=_settings(), uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    result = await service.synthesize(character_id="aiko", text="嗨")
    assert result.cached is False
    assert result.audio_url.startswith("/uploads/tts/aiko/")
    assert result.audio_url.endswith(".wav")
    rel = result.audio_url.removeprefix("/uploads/")
    assert rel.startswith("tts/aiko/")


@pytest.mark.asyncio
async def test_second_call_with_same_text_hits_cache(tmp_path: Path) -> None:
    port = _CountingPort()
    service = TTSService(
        port=port, settings=_settings(), uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    a = await service.synthesize(character_id="aiko", text="嗨")
    b = await service.synthesize(character_id="aiko", text="嗨")
    assert port.calls == 1
    assert a.audio_url == b.audio_url
    assert a.cached is False
    assert b.cached is True


@pytest.mark.asyncio
async def test_tts_usage_records_synth_and_cache_hit(tmp_path: Path) -> None:
    port = _CountingPort()
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service = TTSService(
        port=port,
        settings=_settings(voice_id="voice-a"),
        uploads_dir=tmp_path,
        object_storage=_storage(),
        usage_recorder=usage_recorder,
    )

    await service.synthesize(character_id="aiko", text="嗨")
    await service.synthesize(character_id="aiko", text="嗨")
    await usage_recorder.flush()

    rows = await usage_events.list_recent()
    assert [row.status for row in rows] == ["cached", "succeeded"]
    cached, synth = rows
    assert cached.cached is True
    assert cached.upstream_request_id == ""
    assert cached.quantity.billable_quantity == 0
    assert cached.cost.amount == 0
    assert synth.cached is False
    assert synth.upstream_request_id == "tts-test-1"
    assert synth.capability == "tts"
    assert synth.feature_key == "tts_synthesis"
    assert synth.voice_id == "voice-a"
    assert synth.quantity.usage_unit == "character"
    assert synth.quantity.input_quantity == 1
    assert synth.quantity.billable_quantity == 1
    assert synth.output_bytes == len(b"WAV-FAKE")


@pytest.mark.asyncio
async def test_tts_usage_records_failed_provider_call(tmp_path: Path) -> None:
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service = TTSService(
        port=_FailingPort(TTSUnavailable("offline")),
        settings=_settings(),
        uploads_dir=tmp_path,
        object_storage=_storage(),
        usage_recorder=usage_recorder,
    )

    with pytest.raises(TTSUnavailable):
        await service.synthesize(character_id="aiko", text="嗨")
    await usage_recorder.flush()

    rows = await usage_events.list_recent()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "TTSUnavailable"
    assert rows[0].quantity.billable_quantity == 1


@pytest.mark.asyncio
async def test_different_text_yields_different_file(tmp_path: Path) -> None:
    port = _CountingPort()
    service = TTSService(
        port=port, settings=_settings(), uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    a = await service.synthesize(character_id="aiko", text="嗨")
    b = await service.synthesize(character_id="aiko", text="嗨～")
    assert port.calls == 2
    assert a.audio_url != b.audio_url


@pytest.mark.asyncio
async def test_voice_config_change_invalidates_cache(tmp_path: Path) -> None:
    """Changing the ref audio (or any voice param) yields a fresh
    fingerprint, so a stale cache from the old config doesn't replay
    when the operator just spent time tuning the voice."""
    port = _CountingPort()
    service_a = TTSService(
        port=port,
        settings=_settings(ref_audio_path="/data/ref-a.wav"),
        uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    a = await service_a.synthesize(character_id="aiko", text="嗨")

    service_b = TTSService(
        port=port,
        settings=_settings(ref_audio_path="/data/ref-b.wav"),
        uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    b = await service_b.synthesize(character_id="aiko", text="嗨")

    assert port.calls == 2
    assert a.audio_url != b.audio_url


@pytest.mark.asyncio
async def test_disabled_settings_raises_unavailable(tmp_path: Path) -> None:
    """Empty config => the route's 503 path; the port is never even
    invoked so a misconfigured deployment doesn't churn HTTP calls."""
    port = _CountingPort()
    service = TTSService(
        port=port,
        settings=TTSSettings(),  # all empty
        uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    with pytest.raises(TTSUnavailable):
        await service.synthesize(character_id="aiko", text="嗨")
    assert port.calls == 0


@pytest.mark.asyncio
async def test_empty_text_raises_error(tmp_path: Path) -> None:
    port = _CountingPort()
    service = TTSService(
        port=port, settings=_settings(), uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    with pytest.raises(TTSError):
        await service.synthesize(character_id="aiko", text="   ")
    assert port.calls == 0


@pytest.mark.asyncio
async def test_port_unavailable_propagates(tmp_path: Path) -> None:
    port = _FailingPort(TTSUnavailable("offline"))
    service = TTSService(
        port=port, settings=_settings(), uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    with pytest.raises(TTSUnavailable):
        await service.synthesize(character_id="aiko", text="嗨")


@pytest.mark.asyncio
async def test_long_text_is_trimmed_at_ceiling(tmp_path: Path) -> None:
    """Inputs above the per-call ceiling are trimmed rather than
    rejected — the user already saw the bubble; we'd rather voice
    most of it than refuse outright."""
    port = _CountingPort()
    service = TTSService(
        port=port, settings=_settings(), uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    long_text = "啊" * 2000
    await service.synthesize(character_id="aiko", text=long_text)
    assert port.last_request is not None
    assert len(port.last_request.text) <= 800


# ---------- translation path ----------


class _ScriptedTranslator(TTSTranslatorPort):
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[tuple[str, str, str]] = []

    async def translate(
        self, *, text: str, source_lang: str, target_lang: str,
    ) -> str:
        self.calls.append((text, source_lang, target_lang))
        return self.mapping.get(text, "")


@pytest.mark.asyncio
async def test_translation_pipeline_swaps_text_and_lang(tmp_path: Path) -> None:
    """When ``translate_target_lang`` is set, the synth request the
    port sees is the translated text under the target language tag."""
    port = _CountingPort()
    translator = _ScriptedTranslator({
        "你今天看起來有點累呢": "今日は少し疲れて見えるね",
    })
    service = TTSService(
        port=port,
        settings=_settings(translate_target_lang="ja"),
        uploads_dir=tmp_path,
        translator=translator,
        object_storage=_storage(),
    )
    await service.synthesize(
        character_id="aiko", text="你今天看起來有點累呢",
    )
    assert port.last_request is not None
    assert port.last_request.text == "今日は少し疲れて見えるね"
    assert port.last_request.text_lang == "ja"
    assert translator.calls == [
        ("你今天看起來有點累呢", "zh", "ja"),
    ]


@pytest.mark.asyncio
async def test_cache_keyed_on_source_text_not_translation(
    tmp_path: Path,
) -> None:
    """Repeating the same source text must hit the cache even though
    the LLM translator is non-deterministic — otherwise a chatty user
    gets translated + synthesized again on every replay."""
    port = _CountingPort()
    translator = _ScriptedTranslator({"嗨": "やあ"})
    service = TTSService(
        port=port,
        settings=_settings(translate_target_lang="ja"),
        uploads_dir=tmp_path,
        translator=translator,
        object_storage=_storage(),
    )
    await service.synthesize(character_id="aiko", text="嗨")
    await service.synthesize(character_id="aiko", text="嗨")
    assert port.calls == 1
    assert len(translator.calls) == 1


@pytest.mark.asyncio
async def test_translator_empty_falls_back_to_source_text(
    tmp_path: Path,
) -> None:
    """LLM unreachable → translator returns "" → service synths the
    source text under source language so the play button still works."""
    port = _CountingPort()
    translator = _ScriptedTranslator({})  # always returns ""
    service = TTSService(
        port=port,
        settings=_settings(translate_target_lang="ja"),
        uploads_dir=tmp_path,
        translator=translator,
        object_storage=_storage(),
    )
    await service.synthesize(character_id="aiko", text="嗨")
    assert port.last_request is not None
    assert port.last_request.text == "嗨"
    assert port.last_request.text_lang == "zh"


@pytest.mark.asyncio
async def test_target_lang_change_busts_cache(tmp_path: Path) -> None:
    port_a = _CountingPort()
    service_a = TTSService(
        port=port_a, settings=_settings(translate_target_lang=""),
        uploads_dir=tmp_path,
        object_storage=_storage(),
    )
    a = await service_a.synthesize(character_id="aiko", text="嗨")

    port_b = _CountingPort()
    service_b = TTSService(
        port=port_b,
        settings=_settings(translate_target_lang="ja"),
        uploads_dir=tmp_path,
        translator=_ScriptedTranslator({"嗨": "やあ"}),
        object_storage=_storage(),
    )
    b = await service_b.synthesize(character_id="aiko", text="嗨")

    assert a.audio_url != b.audio_url
    assert port_a.calls == 1
    assert port_b.calls == 1


@pytest.mark.asyncio
async def test_target_same_as_source_skips_translation(
    tmp_path: Path,
) -> None:
    """Setting ``translate_target_lang`` equal to ``text_lang`` is a
    no-op — translator never gets called, output language unchanged."""
    port = _CountingPort()
    translator = _ScriptedTranslator({"嗨": "WRONG"})
    service = TTSService(
        port=port,
        settings=_settings(translate_target_lang="zh"),  # same as text_lang
        uploads_dir=tmp_path,
        translator=translator,
        object_storage=_storage(),
    )
    await service.synthesize(character_id="aiko", text="嗨")
    assert translator.calls == []
    assert port.last_request.text == "嗨"
    assert port.last_request.text_lang == "zh"


# ---------- per-character voice profile ----------


def _make_test_character(
    *, character_id: str = "aiko",
    voice_profile=None,
):
    from dataclasses import replace
    from kokoro_link.domain.entities.character import Character
    from kokoro_link.domain.value_objects.character_state import CharacterState

    char = Character.create(
        name="Aiko", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
        ),
    )
    char = replace(char, id=character_id, voice_profile=voice_profile)
    return char


class _StaticCharRepo:
    def __init__(self, character) -> None:
        self._char = character

    async def get(self, character_id: str):
        return self._char if self._char and self._char.id == character_id else None

    async def list(self):
        return [self._char] if self._char else []

    async def save(self, character):
        self._char = character


@pytest.mark.asyncio
async def test_character_voice_profile_overrides_global(
    tmp_path: Path,
) -> None:
    """A character with a populated VoiceProfile uses its values, not
    the global ones."""
    from kokoro_link.domain.value_objects.voice_profile import VoiceProfile

    port = _CountingPort()
    char = _make_test_character(voice_profile=VoiceProfile(
        ref_audio_path="/per-char/ref.wav",
        prompt_text="角色專屬 prompt",
        prompt_lang="ja",
    ))
    service = TTSService(
        port=port,
        settings=_settings(
            ref_audio_path="/global/ref.wav",
            prompt_text="全域 prompt",
            prompt_lang="zh",
        ),
        uploads_dir=tmp_path,
        character_repository=_StaticCharRepo(char),
        object_storage=_storage(),
    )
    await service.synthesize(character_id=char.id, text="hi")
    assert port.last_request.ref_audio_path == "/per-char/ref.wav"
    assert port.last_request.prompt_text == "角色專屬 prompt"
    assert port.last_request.prompt_lang == "ja"


@pytest.mark.asyncio
async def test_character_partial_profile_inherits_blanks(
    tmp_path: Path,
) -> None:
    """A profile that only overrides ``prompt_lang`` keeps the global
    ref + prompt — partial overrides are useful when 大半 characters
    share the global voice but one wants different lang routing."""
    from kokoro_link.domain.value_objects.voice_profile import VoiceProfile

    port = _CountingPort()
    char = _make_test_character(voice_profile=VoiceProfile(
        prompt_lang="en",  # only this is set
    ))
    service = TTSService(
        port=port,
        settings=_settings(
            ref_audio_path="/global/ref.wav",
            prompt_text="全域 prompt",
            prompt_lang="zh",
        ),
        uploads_dir=tmp_path,
        character_repository=_StaticCharRepo(char),
        object_storage=_storage(),
    )
    await service.synthesize(character_id=char.id, text="hi")
    assert port.last_request.ref_audio_path == "/global/ref.wav"
    assert port.last_request.prompt_text == "全域 prompt"
    assert port.last_request.prompt_lang == "en"


@pytest.mark.asyncio
async def test_character_profile_disabled_raises_unavailable(
    tmp_path: Path,
) -> None:
    """``profile.enabled=False`` greys out TTS for that character even
    when global is configured."""
    from kokoro_link.domain.value_objects.voice_profile import VoiceProfile

    port = _CountingPort()
    char = _make_test_character(voice_profile=VoiceProfile(enabled=False))
    service = TTSService(
        port=port,
        settings=_settings(),
        uploads_dir=tmp_path,
        character_repository=_StaticCharRepo(char),
        object_storage=_storage(),
    )
    with pytest.raises(TTSUnavailable):
        await service.synthesize(character_id=char.id, text="hi")
    assert port.calls == 0


@pytest.mark.asyncio
async def test_demo_runtime_profile_disables_tts_before_provider_call(
    tmp_path: Path,
) -> None:
    port = _CountingPort()
    char = _make_test_character()
    service = TTSService(
        port=port,
        settings=_settings(),
        uploads_dir=tmp_path,
        character_repository=_StaticCharRepo(char),
        object_storage=_storage(),
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
    )

    with pytest.raises(TTSUnavailable):
        await service.synthesize(character_id=char.id, text="hi")

    assert port.calls == 0


@pytest.mark.asyncio
async def test_per_character_weights_propagate_to_request(
    tmp_path: Path,
) -> None:
    """When the profile pins GPT/SoVITS weights, the TTSRequest carries
    them so the adapter can hot-swap before synth."""
    from kokoro_link.domain.value_objects.voice_profile import VoiceProfile

    port = _CountingPort()
    char = _make_test_character(voice_profile=VoiceProfile(
        ref_audio_path="/c/ref.wav", prompt_text="p", prompt_lang="ja",
        gpt_weights_path="GPT_weights_v4/x.ckpt",
        sovits_weights_path="SoVITS_weights_v4/y.pth",
    ))
    service = TTSService(
        port=port,
        settings=_settings(),
        uploads_dir=tmp_path,
        character_repository=_StaticCharRepo(char),
        object_storage=_storage(),
    )
    await service.synthesize(character_id=char.id, text="hi")
    assert port.last_request.weights.gpt_weights_path == "GPT_weights_v4/x.ckpt"
    assert port.last_request.weights.sovits_weights_path == "SoVITS_weights_v4/y.pth"


@pytest.mark.asyncio
async def test_global_blank_filled_by_profile_works(tmp_path: Path) -> None:
    """Global has only ``base_url`` (no ref/prompt) but the character's
    profile fills in the rest — synth proceeds normally."""
    from kokoro_link.domain.value_objects.voice_profile import VoiceProfile

    port = _CountingPort()
    char = _make_test_character(voice_profile=VoiceProfile(
        ref_audio_path="/c/ref.wav",
        prompt_text="角色 prompt",
        prompt_lang="ja",
    ))
    service = TTSService(
        port=port,
        settings=TTSSettings(base_url="http://localhost:9880"),
        uploads_dir=tmp_path,
        character_repository=_StaticCharRepo(char),
        object_storage=_storage(),
    )
    await service.synthesize(character_id=char.id, text="hi")
    assert port.last_request.ref_audio_path == "/c/ref.wav"
