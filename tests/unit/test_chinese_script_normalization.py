"""Simplified→Traditional output normalisation: gate, conversion,
adapter decorator, and the provider binding that ties them together.

The behaviour under test is a safety net for a model failure mode we
cannot prompt away, so the tests care as much about what it must NOT
touch (Japanese, Simplified-Chinese players, streaming) as about what
it converts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.output_language_policy import (
    OperatorOutputLanguageResolver,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.llm.script_normalizer import (
    ScriptNormalizingChatModel,
    bind_script_normalization,
)
from kokoro_link.infrastructure.localization.chinese_script import (
    converter_available,
    normalize_to_traditional,
    targets_traditional_chinese,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)

requires_opencc = pytest.mark.skipif(
    not converter_available(),
    reason="opencc backend not installed — conversion degrades to pass-through",
)


# ---- language gate -----------------------------------------------------


@pytest.mark.parametrize(
    "tag", ["zh-TW", "zh-tw", "zh_TW", "zh-Hant", " zh-Hant-TW "],
)
def test_traditional_tags_are_normalised(tag: str) -> None:
    assert targets_traditional_chinese(tag) is True


@pytest.mark.parametrize(
    "tag",
    [
        "ja",           # shinjitai shares code points with Simplified forms
        "ja-JP",
        "en",
        "en-US",
        "zh-CN",        # the player asked for Simplified
        "zh-Hans",
        "zh-HK",        # Traditional, but s2tw targets Taiwan variants
        "",
        None,
    ],
)
def test_other_tags_are_left_alone(tag: str | None) -> None:
    assert targets_traditional_chinese(tag) is False


# ---- conversion --------------------------------------------------------


@requires_opencc
def test_converts_simplified_to_traditional() -> None:
    assert normalize_to_traditional("这是一个测试") == "這是一個測試"


@requires_opencc
def test_disambiguates_one_to_many_mappings_by_phrase() -> None:
    """发/干/后 each map to several Traditional characters; the right one
    is only decidable from the surrounding word."""
    converted = normalize_to_traditional("头发很长，干净的桌子，他干了大事，皇后在后面")
    assert "頭髮" in converted
    assert "乾淨" in converted
    assert "他幹了" in converted
    assert "皇后" in converted
    assert "後面" in converted


@requires_opencc
def test_already_traditional_text_is_unchanged() -> None:
    original = "這已經是繁體中文了，不該被改動。"
    assert normalize_to_traditional(original) == original


@requires_opencc
def test_structure_and_ascii_survive() -> None:
    """Tool-call JSON passes through this layer; the envelope must not
    move, only Han characters inside string values."""
    converted = normalize_to_traditional(
        '{"tool": "web_search", "args": {"query": "台北天气"}}',
    )
    assert converted == '{"tool": "web_search", "args": {"query": "台北天氣"}}'


@requires_opencc
def test_place_names_keep_everyday_taiwanese_form() -> None:
    """OpenCC standardises 台 → 臺, which reads like paperwork in
    dialogue. The fold back applies to converted text only."""
    assert normalize_to_traditional("我在台北的天气不错") == "我在台北的天氣不錯"


@requires_opencc
def test_untouched_traditional_text_escapes_the_colloquial_fold() -> None:
    """A reply that needed no conversion must survive byte-identical —
    including a deliberate 臺, which only the fold could have moved."""
    original = "臺灣銀行今天休息"
    assert normalize_to_traditional(original) == original


@requires_opencc
def test_reporting_flag_marks_only_real_drift() -> None:
    from kokoro_link.infrastructure.localization.chinese_script import (
        normalize_to_traditional_reporting,
    )

    _, drifted = normalize_to_traditional_reporting("这是简体")
    assert drifted is True
    _, clean = normalize_to_traditional_reporting("這是繁體")
    assert clean is False


def test_empty_text_is_returned_as_is() -> None:
    assert normalize_to_traditional("") == ""


# ---- adapter decorator -------------------------------------------------


class _StubModel(ChatModelPort):
    def __init__(self, reply: str = "这是回复") -> None:
        self.provider_id = "stub"
        self.supports_vision = True
        self.prefers_public_image_urls = True
        self._reply = reply
        self.stream_chunks = ("这是", "回复")
        self.custom_marker = "inner-only"

    async def generate(
        self, prompt: str, *,
        image_urls: Sequence[str] = (), model: str | None = None,
    ) -> str:
        return self._reply

    async def generate_stream(
        self, prompt: str, *,
        image_urls: Sequence[str] = (), model: str | None = None,
    ) -> AsyncIterator[str]:
        for chunk in self.stream_chunks:
            yield chunk

    async def list_models(self) -> list[str]:
        return ["stub-model"]


@requires_opencc
@pytest.mark.asyncio
async def test_decorator_converts_non_streaming_replies() -> None:
    model = ScriptNormalizingChatModel(_StubModel())
    assert await model.generate("prompt") == "這是回覆"


@pytest.mark.asyncio
async def test_decorator_leaves_streaming_untouched() -> None:
    """Phrase-level disambiguation cannot survive chunk boundaries, so
    the streaming path is delegated verbatim until it grows its own
    sentence buffer."""
    model = ScriptNormalizingChatModel(_StubModel())
    chunks = [chunk async for chunk in model.generate_stream("prompt")]
    assert chunks == ["这是", "回复"]


@pytest.mark.asyncio
async def test_decorator_forwards_port_attributes_and_capabilities() -> None:
    inner = _StubModel()
    model = ScriptNormalizingChatModel(inner)
    assert model.provider_id == "stub"
    assert model.supports_vision is True
    assert model.prefers_public_image_urls is True
    assert await model.list_models() == ["stub-model"]
    # Duck-typed capability probing must still see through the wrapper,
    # otherwise binding it silently disables metadata capture / routing
    # overrides for every player it is bound for.
    assert model.custom_marker == "inner-only"


def test_binding_is_skipped_for_other_languages() -> None:
    inner = _StubModel()
    assert bind_script_normalization(inner, language_tag="ja") is inner
    assert bind_script_normalization(inner, language_tag=None) is inner
    assert isinstance(
        bind_script_normalization(inner, language_tag="zh-TW"),
        ScriptNormalizingChatModel,
    )


# ---- language resolution ----------------------------------------------


class _StubOperatorRepository:
    def __init__(self, profiles: dict[str, OperatorProfile]) -> None:
        self._profiles = profiles
        self.reads = 0

    async def get(self, operator_id: str) -> OperatorProfile | None:
        self.reads += 1
        return self._profiles.get(operator_id)


class _ExplodingOperatorRepository:
    async def get(self, operator_id: str) -> OperatorProfile | None:
        raise RuntimeError("database is down")


def _profile(operator_id: str, language: str) -> OperatorProfile:
    return OperatorProfile(
        id=operator_id, display_name="測試", primary_language=language,
    )


def _character(user_id: str = "owner-1") -> Character:
    return Character.create(
        name="Yuki", summary="測試角色",
        personality=["calm"], interests=["music"],
        speaking_style="soft", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_language_resolves_from_explicit_operator_id() -> None:
    repository = _StubOperatorRepository({"actor": _profile("actor", "ja")})
    resolver = OperatorOutputLanguageResolver(repository=repository)
    assert await resolver.resolve(operator_id="actor") == "ja"


@pytest.mark.asyncio
async def test_language_falls_back_to_character_owner() -> None:
    repository = _StubOperatorRepository(
        {"owner-1": _profile("owner-1", "zh-TW")},
    )
    resolver = OperatorOutputLanguageResolver(repository=repository)
    assert await resolver.resolve(character=_character()) == "zh-TW"


@pytest.mark.asyncio
async def test_explicit_operator_wins_over_character_owner() -> None:
    repository = _StubOperatorRepository({
        "owner-1": _profile("owner-1", "zh-TW"),
        "actor": _profile("actor", "en"),
    })
    resolver = OperatorOutputLanguageResolver(repository=repository)
    resolved = await resolver.resolve(
        character=_character(), operator_id="actor",
    )
    assert resolved == "en"


@pytest.mark.asyncio
async def test_repeat_lookups_are_cached_and_invalidatable() -> None:
    repository = _StubOperatorRepository({"actor": _profile("actor", "zh-TW")})
    resolver = OperatorOutputLanguageResolver(repository=repository)
    await resolver.resolve(operator_id="actor")
    await resolver.resolve(operator_id="actor")
    assert repository.reads == 1
    resolver.invalidate("actor")
    await resolver.resolve(operator_id="actor")
    assert repository.reads == 2


@pytest.mark.asyncio
async def test_unknown_operator_and_lookup_failure_yield_no_language() -> None:
    """Both degrade to "" so the caller normalises nothing — guessing a
    language is worse than shipping the model's own output."""
    empty = OperatorOutputLanguageResolver(repository=_StubOperatorRepository({}))
    assert await empty.resolve(operator_id="ghost") == ""
    assert await empty.resolve() == ""

    broken = OperatorOutputLanguageResolver(
        repository=_ExplodingOperatorRepository(),
    )
    assert await broken.resolve(operator_id="actor") == ""


# ---- provider binding --------------------------------------------------


def _provider(
    language: str, *, model: ChatModelPort,
) -> PreferenceBackedActiveLLMProvider:
    registry = InMemoryChatModelRegistry(default_provider_id=model.provider_id)
    registry.register(model)
    repository = _StubOperatorRepository(
        {"owner-1": _profile("owner-1", language)},
    )
    return PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=InMemoryPreferencesRepository(),
        default_provider_id=model.provider_id,
        output_language_resolver=OperatorOutputLanguageResolver(
            repository=repository,
        ),
    )


@requires_opencc
@pytest.mark.asyncio
async def test_resolved_model_normalises_for_traditional_players() -> None:
    provider = _provider("zh-TW", model=_StubModel("这是回复"))
    resolved = await provider.resolve("chat", character=_character())
    assert await resolved.generate("prompt") == "這是回覆"


@requires_opencc
@pytest.mark.asyncio
async def test_resolved_model_leaves_japanese_players_alone() -> None:
    """s2tw would rewrite shinjitai into Chinese glyphs (学→學, 国→國),
    so a Japanese player must never be bound."""
    japanese = "学校の国語の授業は体育館で行われた"
    provider = _provider("ja", model=_StubModel(japanese))
    resolved = await provider.resolve("chat", character=_character())
    assert await resolved.generate("prompt") == japanese


@requires_opencc
@pytest.mark.asyncio
async def test_hosted_provider_also_normalises() -> None:
    """Hosted players are the most exposed — their tier preset decides
    the upstream, so drift can appear without anyone changing a setting."""
    from kokoro_link.application.services.cloud_active_llm_provider import (
        CloudActiveLLMProvider,
    )
    from kokoro_link.application.services.cloud_identity_resolver import (
        CloudOperatorIdentityResolver,
    )
    from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
        InMemoryOperatorProfileRepository,
    )

    repository = InMemoryOperatorProfileRepository()
    await repository.save(
        OperatorProfile(
            id="cloud:acct_1",
            display_name="Player",
            primary_language="zh-TW",
            cloud_account_id="acct_1",
            cloud_tenant_id="tenant_1",
            auth_provider="cloud",
        ),
    )
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repository),
        model_factory=lambda feature_key, identity, preset: _StubModel("这是回复"),
        model_presets={"default": "preset-default"},
        output_language_resolver=OperatorOutputLanguageResolver(
            repository=repository,
        ),
    )

    resolved = await provider.resolve("chat", character=_character("cloud:acct_1"))

    assert await resolved.generate("prompt") == "這是回覆"


@pytest.mark.asyncio
async def test_provider_without_resolver_binds_nothing() -> None:
    model = _StubModel("这是回复")
    registry = InMemoryChatModelRegistry(default_provider_id=model.provider_id)
    registry.register(model)
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=InMemoryPreferencesRepository(),
        default_provider_id=model.provider_id,
    )
    resolved = await provider.resolve("chat", character=_character())
    assert await resolved.generate("prompt") == "这是回复"
