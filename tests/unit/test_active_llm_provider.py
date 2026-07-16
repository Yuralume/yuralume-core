"""Tests for ``PreferenceBackedActiveLLMProvider`` + end-to-end check that
auxiliary LLM services actually route through the operator's UI model
pick rather than the container-time default.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.application.services.active_llm_provider import (
    ROUTING_SOURCE_NSFW_CONTENT,
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.nsfw_mode import NsfwModeService
from kokoro_link.application.services.scoped_preferences import set_user_preference
from kokoro_link.contracts.llm import ChatModelPort, ReasoningOverrides
from kokoro_link.domain.entities.character import (
    Character, FeatureModelOverride,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.goal.llm_reviewer import LLMGoalReviewer
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.post_turn.llm_processor import (
    LLMPostTurnProcessor,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)


def _character() -> Character:
    return Character.create(
        name="Yuki", summary="測試角色",
        personality=["calm"], interests=["music"],
        speaking_style="soft", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


class _RecordingModel(ChatModelPort):
    """Minimal ChatModelPort that records the (prompt, model_id) pairs it
    was called with so tests can assert which provider handled each call.
    """

    def __init__(self, provider_id: str, *, reply: str = "{}") -> None:
        self.provider_id = provider_id
        self.supports_vision = False
        self._reply = reply
        self.calls: list[tuple[str, str | None]] = []

    async def generate(
        self, prompt: str, *,
        image_urls: Sequence[str] = (), model: str | None = None,
    ) -> str:
        self.calls.append((prompt, model))
        return self._reply

    async def generate_stream(
        self, prompt: str, *,
        image_urls: Sequence[str] = (), model: str | None = None,
    ) -> AsyncIterator[str]:
        await self.generate(prompt)
        async def _iter() -> AsyncIterator[str]:
            yield self._reply
        return _iter()

    async def list_models(self) -> list[str]:
        return [self.provider_id]


def _wire() -> tuple[
    PreferenceBackedActiveLLMProvider,
    InMemoryPreferencesRepository,
    _RecordingModel, _RecordingModel,
]:
    registry = InMemoryChatModelRegistry(default_provider_id="lmstudio")
    lmstudio = _RecordingModel("lmstudio", reply='{"memories": []}')
    anthropic = _RecordingModel("anthropic", reply='{"memories": []}')
    registry.register(lmstudio)
    registry.register(anthropic)
    prefs = InMemoryPreferencesRepository()
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry, preferences=prefs,
        default_provider_id="lmstudio",
    )
    return provider, prefs, lmstudio, anthropic


def _wire_with_nsfw_mode() -> tuple[
    PreferenceBackedActiveLLMProvider,
    NsfwModeService,
    InMemoryPreferencesRepository,
    _RecordingModel,
    _RecordingModel,
]:
    registry = InMemoryChatModelRegistry(default_provider_id="lmstudio")
    lmstudio = _RecordingModel("lmstudio", reply='{"memories": []}')
    anthropic = _RecordingModel("anthropic", reply='{"memories": []}')
    registry.register(lmstudio)
    registry.register(anthropic)
    prefs = InMemoryPreferencesRepository()
    nsfw = NsfwModeService(preferences=prefs, ttl_seconds=60)
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=prefs,
        default_provider_id="lmstudio",
        nsfw_mode_service=nsfw,
    )
    return provider, nsfw, prefs, lmstudio, anthropic


async def _set_nsfw_target(
    nsfw: NsfwModeService,
    *,
    llm_provider_id: str = "anthropic",
    llm_model_id: str = "community-nsfw",
    image_profile_id: str = "anime_nsfw",
) -> None:
    await nsfw.set_global_target(
        llm_provider_id=llm_provider_id,
        llm_model_id=llm_model_id,
        image_profile_id=image_profile_id,
    )


def _wire_byok_first() -> tuple[
    PreferenceBackedActiveLLMProvider,
    InMemoryPreferencesRepository,
    _RecordingModel,
    _RecordingModel,
]:
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    fake = _RecordingModel("fake", reply="fake")
    openai = _RecordingModel("openai", reply='{"memories": []}')
    registry.register(fake)
    registry.register(openai)
    prefs = InMemoryPreferencesRepository()
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=prefs,
        default_provider_id="fake",
    )
    return provider, prefs, fake, openai


def _wire_stale_default() -> tuple[
    PreferenceBackedActiveLLMProvider,
    InMemoryPreferencesRepository,
    _RecordingModel,
]:
    registry = InMemoryChatModelRegistry(default_provider_id="lmstudio")
    anthropic = _RecordingModel("anthropic", reply='{"memories": []}')
    registry.register(anthropic)
    prefs = InMemoryPreferencesRepository()
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=prefs,
        default_provider_id="lmstudio",
    )
    return provider, prefs, anthropic


@pytest.mark.asyncio
async def test_resolve_falls_back_to_default_when_no_preference_saved() -> None:
    provider, _, lmstudio, anthropic = _wire()
    model = await provider.resolve()
    assert model is lmstudio
    assert anthropic.calls == []


@pytest.mark.asyncio
async def test_byok_first_install_uses_first_real_provider_without_env_default() -> None:
    provider, _, fake, openai = _wire_byok_first()

    model = await provider.resolve()

    assert model is openai
    assert model is not fake
    assert await provider.is_fake() is False


@pytest.mark.asyncio
async def test_byok_first_install_keeps_fake_when_no_real_provider_exists() -> None:
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    fake = _RecordingModel("fake", reply="fake")
    registry.register(fake)
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=InMemoryPreferencesRepository(),
        default_provider_id="fake",
    )

    assert await provider.resolve() is fake
    assert await provider.is_fake() is True


@pytest.mark.asyncio
async def test_resolve_uses_saved_preference_when_present() -> None:
    provider, prefs, lmstudio, anthropic = _wire()
    await prefs.set(
        "active_model",
        {"provider_id": "anthropic", "model_id": "claude-sonnet-4-5"},
    )
    model = await provider.resolve()
    assert model is anthropic
    assert await provider.resolve_model_id() == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_nsfw_mode_overrides_character_and_global_model_routes() -> None:
    provider, nsfw, prefs, lmstudio, anthropic = _wire_with_nsfw_mode()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "global"},
    )
    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        feature_models=[
            FeatureModelOverride(
                feature_key="chat",
                provider_id="lmstudio",
                model_id="character-local",
            ),
        ],
        user_id="alice",
    )
    await _set_nsfw_target(nsfw)
    await nsfw.enable(user_id="alice")

    assert await provider.resolve("chat", character=character) is anthropic
    assert (
        await provider.resolve_model_id("chat", character=character)
        == "community-nsfw"
    )
    assert (
        await provider.resolve_routing_source("chat", character=character)
        == "nsfw_mode"
    )
    assert lmstudio.calls == []


@pytest.mark.asyncio
async def test_expired_nsfw_mode_falls_back_to_normal_routing() -> None:
    from kokoro_link.application.services.scoped_preferences import (
        user_preference_key,
    )

    provider, nsfw, prefs, lmstudio, anthropic = _wire_with_nsfw_mode()
    await _set_nsfw_target(nsfw)
    await prefs.set(
        user_preference_key("alice", "nsfw_mode"),
        {
            "active": True,
            "last_activity_at": "2020-01-01T00:00:00+00:00",
        },
    )
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "global"},
    )
    character = Character.create(
        name="Yuki", summary="", user_id="alice",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )

    assert await provider.resolve("chat", character=character) is lmstudio
    assert await provider.resolve_model_id("chat", character=character) == "global"
    assert anthropic.calls == []


@pytest.mark.asyncio
async def test_rule_b_community_hint_uses_configured_nsfw_target_after_expiry() -> None:
    from kokoro_link.application.services.scoped_preferences import (
        user_preference_key,
    )
    from kokoro_link.domain.value_objects.content_flow import (
        CONTENT_TOLERANCE_COMMUNITY,
    )

    provider, nsfw, prefs, lmstudio, anthropic = _wire_with_nsfw_mode()
    await _set_nsfw_target(nsfw)
    await prefs.set(
        user_preference_key("alice", "nsfw_mode"),
        {
            "active": True,
            "last_activity_at": "2020-01-01T00:00:00+00:00",
        },
    )
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "global"},
    )
    character = Character.create(
        name="Yuki", summary="", user_id="alice",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )

    assert (
        await provider.resolve(
            "busy_follow_up",
            character=character,
            content_tolerance=CONTENT_TOLERANCE_COMMUNITY,
        )
        is anthropic
    )
    assert (
        await provider.resolve_model_id(
            "busy_follow_up",
            character=character,
            content_tolerance=CONTENT_TOLERANCE_COMMUNITY,
        )
        == "community-nsfw"
    )
    assert (
        await provider.resolve_routing_source(
            "busy_follow_up",
            character=character,
            content_tolerance=CONTENT_TOLERANCE_COMMUNITY,
        )
        == ROUTING_SOURCE_NSFW_CONTENT
    )
    assert lmstudio.calls == []


@pytest.mark.asyncio
async def test_stale_nsfw_provider_refuses_runtime_fallback() -> None:
    provider, nsfw, _, _, _ = _wire_with_nsfw_mode()
    character = Character.create(
        name="Yuki", summary="", user_id="alice",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    await _set_nsfw_target(
        nsfw,
        llm_provider_id="ghost",
        llm_model_id="ghost-model",
        image_profile_id="anime_nsfw",
    )
    await nsfw.enable(user_id="alice")

    with pytest.raises(RuntimeError):
        await provider.resolve("chat", character=character)


@pytest.mark.asyncio
async def test_character_user_preference_overrides_global_preference() -> None:
    provider, prefs, lmstudio, anthropic = _wire()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "global"},
    )
    await set_user_preference(
        prefs,
        "active_model",
        {"provider_id": "anthropic", "model_id": "user-model"},
        user_id="alice",
    )
    alice_character = Character.create(
        name="Alice Character",
        summary="",
        user_id="alice",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    bob_character = Character.create(
        name="Bob Character",
        summary="",
        user_id="bob",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )

    assert await provider.resolve(character=alice_character) is anthropic
    assert await provider.resolve_model_id(character=alice_character) == "user-model"
    assert await provider.resolve(character=bob_character) is lmstudio
    assert await provider.resolve_model_id(character=bob_character) == "global"


@pytest.mark.asyncio
async def test_resolve_falls_back_when_preference_points_at_unknown_provider() -> None:
    provider, prefs, lmstudio, _ = _wire()
    await prefs.set(
        "active_model",
        {"provider_id": "openai", "model_id": "gpt-5"},
    )
    model = await provider.resolve()
    # Unknown provider → degrade to default, don't crash the turn.
    assert model is lmstudio


@pytest.mark.asyncio
async def test_is_fake_follows_preferred_provider() -> None:
    provider, prefs, _, _ = _wire()
    # No preference saved yet — default is lmstudio (real).
    assert await provider.is_fake() is False
    await prefs.set(
        "active_model", {"provider_id": "fake", "model_id": None},
    )
    assert await provider.is_fake() is True


# ---------------------------------------------------------------------------
# End-to-end: does the post-turn / goal processor actually pick the new
# model when the preference changes mid-session?
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_turn_processor_routes_through_preferred_provider() -> None:
    """Boot with lmstudio as default → switch preference to anthropic →
    the very next post-turn call lands on anthropic. That's the
    regression fix: previously the processor was frozen at the default
    and never saw the dropdown flip.
    """
    provider, prefs, lmstudio, anthropic = _wire()
    processor = LLMPostTurnProcessor(provider=provider)

    # First call — no preference saved → default lmstudio.
    character = _character()
    await processor.process(
        character=character, conversation_id="c1",
        user_message="嗨", assistant_message="你好",
    )
    assert len(lmstudio.calls) == 1
    assert anthropic.calls == []

    # Flip preference → next call should land on anthropic, with the
    # ``model=`` kwarg forwarding the saved model_id.
    await prefs.set(
        "active_model",
        {"provider_id": "anthropic", "model_id": "claude-sonnet-4-5"},
    )
    character2 = _character()
    await processor.process(
        character=character2, conversation_id="c1",
        user_message="再一次", assistant_message="好",
    )
    assert len(anthropic.calls) == 1
    # lmstudio wasn't called a second time.
    assert len(lmstudio.calls) == 1
    # The preferred model_id rode along on the generate call.
    assert anthropic.calls[0][1] == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_feature_override_takes_precedence_over_global_active_model() -> None:
    """Preference shape::

        active_model = {provider: lmstudio, model: a}
        feature_models = {post_turn: {provider: anthropic, model: b}}

    ``resolve(post_turn)`` → anthropic;
    ``resolve(goal_review)`` → lmstudio (inherits global);
    ``resolve()`` → lmstudio (no feature key at all).

    This is the core of per-feature routing: operators can pin an
    expensive model on the main chat while routing noisy background
    extraction to a cheaper local one, or vice-versa.
    """
    provider, prefs, lmstudio, anthropic = _wire()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "a"},
    )
    await prefs.set(
        "feature_models",
        {"post_turn": {"provider_id": "anthropic", "model_id": "b"}},
    )

    assert await provider.resolve("post_turn") is anthropic
    assert await provider.resolve_model_id("post_turn") == "b"

    # Goal review has no override → inherits global.
    assert await provider.resolve("goal_review") is lmstudio
    assert await provider.resolve_model_id("goal_review") == "a"

    # No feature key at all → global.
    assert await provider.resolve() is lmstudio
    assert await provider.resolve_model_id() == "a"


@pytest.mark.asyncio
async def test_group_override_fills_gap_between_feature_and_active_model() -> None:
    provider, prefs, lmstudio, anthropic = _wire()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "active"},
    )
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "anthropic",
                "model_id": "group-memory",
            },
        },
    )

    assert await provider.resolve("post_turn") is anthropic
    assert await provider.resolve_model_id("post_turn") == "group-memory"
    assert (
        await provider.resolve_routing_source("post_turn")
        == "global_group"
    )

    assert await provider.resolve("chat") is lmstudio
    assert await provider.resolve_model_id("chat") == "active"


@pytest.mark.asyncio
async def test_feature_override_takes_precedence_over_group_override() -> None:
    provider, prefs, lmstudio, anthropic = _wire()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "lmstudio",
                "model_id": "group-memory",
            },
        },
    )
    await prefs.set(
        "feature_models",
        {"post_turn": {"provider_id": "anthropic", "model_id": "feature"}},
    )

    assert await provider.resolve("post_turn") is anthropic
    assert await provider.resolve_model_id("post_turn") == "feature"
    assert (
        await provider.resolve_routing_source("post_turn")
        == "global_feature"
    )


@pytest.mark.asyncio
async def test_character_override_takes_precedence_over_group_override() -> None:
    provider, prefs, lmstudio, anthropic = _wire()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "anthropic",
                "model_id": "group-memory",
            },
        },
    )
    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        feature_models=[
            FeatureModelOverride(
                feature_key="post_turn",
                provider_id="lmstudio", model_id="character",
            ),
        ],
    )

    assert await provider.resolve("post_turn", character=character) is lmstudio
    assert (
        await provider.resolve_model_id("post_turn", character=character)
        == "character"
    )
    assert (
        await provider.resolve_routing_source("post_turn", character=character)
        == "character_feature"
    )


@pytest.mark.asyncio
async def test_unknown_feature_key_skips_group_override() -> None:
    provider, prefs, lmstudio, anthropic = _wire()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "active"},
    )
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "anthropic",
                "model_id": "group-memory",
            },
        },
    )

    assert await provider.resolve("typo_feature") is lmstudio
    assert await provider.resolve_model_id("typo_feature") == "active"
    assert (
        await provider.resolve_routing_source("typo_feature")
        == "active_model"
    )


@pytest.mark.asyncio
async def test_group_override_with_only_provider_inherits_active_model_id() -> None:
    provider, prefs, _, anthropic = _wire()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "active"},
    )
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "anthropic",
                "model_id": None,
            },
        },
    )

    assert await provider.resolve("post_turn") is anthropic
    assert await provider.resolve_model_id("post_turn") == "active"


@pytest.mark.asyncio
async def test_stale_group_provider_reports_runtime_fallback_source() -> None:
    provider, prefs, anthropic = _wire_stale_default()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "lmstudio",
                "model_id": "local-only-model",
            },
        },
    )

    assert await provider.resolve("post_turn") is anthropic
    assert await provider.resolve_routing_source("post_turn") == "runtime_fallback"


@pytest.mark.asyncio
async def test_feature_override_with_only_provider_inherits_global_model() -> None:
    """Half-populated feature entry: provider pinned, model blank.

    Operators sometimes want "use anthropic for memory extraction but
    whatever model the primary dropdown chose" — e.g. when they flip
    between Sonnet/Opus globally and want extraction to follow. In
    that case the per-feature entry has ``provider_id`` set but
    ``model_id=null``. ``resolve_model_id`` falls through to the
    global ``active_model.model_id``.
    """
    provider, prefs, _, anthropic = _wire()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "global-model"},
    )
    await prefs.set(
        "feature_models",
        {"post_turn": {"provider_id": "anthropic", "model_id": None}},
    )
    assert await provider.resolve("post_turn") is anthropic
    # No pinned feature model → global model id bubbles up.
    assert await provider.resolve_model_id("post_turn") == "global-model"


@pytest.mark.asyncio
async def test_feature_override_with_unknown_provider_degrades_to_default() -> None:
    """Stale preference pointing at a provider that's been removed
    shouldn't crash — degrade to the container default."""
    provider, prefs, lmstudio, _ = _wire()
    await prefs.set(
        "feature_models",
        {"post_turn": {"provider_id": "openai", "model_id": "gpt-5"}},
    )
    # openai isn't in the registry — resolve falls back to default.
    assert await provider.resolve("post_turn") is lmstudio


@pytest.mark.asyncio
async def test_stale_active_model_provider_does_not_leak_model_id_to_fallback() -> None:
    provider, prefs, anthropic = _wire_stale_default()
    await prefs.set(
        "active_model",
        {"provider_id": "lmstudio", "model_id": "local-only-model"},
    )

    assert await provider.resolve() is anthropic
    assert await provider.resolve_model_id() is None


@pytest.mark.asyncio
async def test_stale_feature_provider_does_not_leak_model_id_to_fallback() -> None:
    provider, prefs, anthropic = _wire_stale_default()
    await prefs.set(
        "active_model",
        {"provider_id": "anthropic", "model_id": "claude-sonnet-4-5"},
    )
    await prefs.set(
        "feature_models",
        {"post_turn": {"provider_id": "lmstudio", "model_id": "local-only-model"}},
    )

    assert await provider.resolve("post_turn") is anthropic
    assert await provider.resolve_model_id("post_turn") == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_character_override_takes_precedence_over_global_feature_models() -> None:
    """Per-character override is the highest priority layer.

    Setup: global ``active_model = lmstudio``, global
    ``feature_models[post_turn] = anthropic`` (would normally route
    post-turn to anthropic). Character pins
    ``feature_models[post_turn] = lmstudio`` so its post-turn calls
    go back to lmstudio.

    Expected: when ``character`` is passed, post-turn lands on lmstudio
    (per-character override wins). When ``character`` is omitted (e.g.
    the wizard intake which has no character), it falls through to the
    global feature override → anthropic.
    """
    provider, prefs, lmstudio, anthropic = _wire()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "global"},
    )
    await prefs.set(
        "feature_models",
        {"post_turn": {"provider_id": "anthropic", "model_id": "globalpt"}},
    )
    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        feature_models=[
            FeatureModelOverride(
                feature_key="post_turn",
                provider_id="lmstudio", model_id="char-specific",
            ),
        ],
    )

    # With character → per-character override wins.
    assert (
        await provider.resolve("post_turn", character=character)
    ) is lmstudio
    assert (
        await provider.resolve_model_id("post_turn", character=character)
    ) == "char-specific"

    # Without character → falls through to the global feature override.
    assert await provider.resolve("post_turn") is anthropic
    assert await provider.resolve_model_id("post_turn") == "globalpt"


@pytest.mark.asyncio
async def test_stale_character_provider_does_not_leak_model_id_to_fallback() -> None:
    provider, prefs, anthropic = _wire_stale_default()
    await prefs.set(
        "active_model",
        {"provider_id": "anthropic", "model_id": "claude-sonnet-4-5"},
    )
    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        feature_models=[
            FeatureModelOverride(
                feature_key="post_turn",
                provider_id="lmstudio",
                model_id="local-only-model",
            ),
        ],
    )

    assert await provider.resolve("post_turn", character=character) is anthropic
    assert (
        await provider.resolve_model_id("post_turn", character=character)
        == "claude-sonnet-4-5"
    )


@pytest.mark.asyncio
async def test_character_override_with_only_provider_inherits_model_chain() -> None:
    """Per-character override with provider pinned but no model_id
    should fall through to the next layer for the model id (here the
    global feature_models[post_turn].model_id), not silently null it
    out — same pattern as the global override's blank-model behaviour.
    """
    provider, prefs, _, anthropic = _wire()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "global"},
    )
    await prefs.set(
        "feature_models",
        {"post_turn": {"provider_id": "anthropic", "model_id": "globalpt"}},
    )
    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        feature_models=[
            FeatureModelOverride(
                feature_key="post_turn",
                provider_id="anthropic", model_id=None,
            ),
        ],
    )
    assert (
        await provider.resolve("post_turn", character=character)
    ) is anthropic
    # provider matches global feature override, model id bubbles from
    # the global feature override since the character row didn't pin one.
    assert (
        await provider.resolve_model_id("post_turn", character=character)
    ) == "globalpt"


@pytest.mark.asyncio
async def test_character_chat_override_routes_through_chat_path() -> None:
    """End-to-end: a character with ``feature_models[chat] = anthropic``
    should make the chat reply land on anthropic even if the global
    dropdown is on lmstudio. This is the user-visible win of the
    feature.
    """
    from kokoro_link.application.dto.chat import SendChatMessageRequest
    from kokoro_link.application.services.chat_service import (
        _resolve_chat_provider_and_model,
    )

    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        feature_models=[
            FeatureModelOverride(
                feature_key="chat",
                provider_id="anthropic", model_id="claude-sonnet-4-5",
            ),
        ],
    )
    payload = SendChatMessageRequest(
        character_id=character.id, message="hi",
        provider_id="lmstudio", model_id="default-local",
    )
    provider_id, model_id = _resolve_chat_provider_and_model(
        character=character, payload=payload,
    )
    assert provider_id == "anthropic"
    assert model_id == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_character_without_chat_override_passes_payload_through() -> None:
    """No chat override → the global dropdown values from the request
    flow through unchanged. Critical for backwards compatibility: every
    existing character keeps behaving as before."""
    from kokoro_link.application.dto.chat import SendChatMessageRequest
    from kokoro_link.application.services.chat_service import (
        _resolve_chat_provider_and_model,
    )

    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    payload = SendChatMessageRequest(
        character_id=character.id, message="hi",
        provider_id="lmstudio", model_id="default-local",
    )
    provider_id, model_id = _resolve_chat_provider_and_model(
        character=character, payload=payload,
    )
    assert provider_id == "lmstudio"
    assert model_id == "default-local"


@pytest.mark.asyncio
async def test_goal_reviewer_shortcircuits_when_preference_is_fake() -> None:
    """Switching the dropdown to the fake provider should make auxiliary
    services skip their LLM call entirely — fake emits non-JSON text
    that would otherwise pollute storage with garbage memory / goal
    extractions.
    """
    provider, prefs, lmstudio, anthropic = _wire()
    reviewer = LLMGoalReviewer(provider=provider)

    await prefs.set(
        "active_model", {"provider_id": "fake", "model_id": None},
    )
    result = await reviewer.review(
        character=_character(), active_goals=[], recent_messages=[],
    )
    assert list(result.status_changes) == []
    assert list(result.new_goals) == []
    # Neither real provider was called.
    assert lmstudio.calls == []
    assert anthropic.calls == []


# ---- routing-level reasoning overrides --------------------------------


class _ReasoningCapableModel(_RecordingModel):
    """Recording model that also supports routing-level reasoning
    binding, mimicking the real openai_compatible / anthropic adapters.
    """

    def __init__(self, provider_id: str, *, reply: str = "{}") -> None:
        super().__init__(provider_id, reply=reply)
        self.bound_overrides: ReasoningOverrides | None = None

    def with_reasoning_overrides(
        self, overrides: ReasoningOverrides,
    ) -> "_ReasoningCapableModel":
        clone = copy.copy(self)
        clone.bound_overrides = overrides
        return clone


def _wire_reasoning() -> tuple[
    PreferenceBackedActiveLLMProvider,
    InMemoryPreferencesRepository,
    _ReasoningCapableModel,
]:
    registry = InMemoryChatModelRegistry(default_provider_id="lmstudio")
    lmstudio = _ReasoningCapableModel("lmstudio", reply='{"memories": []}')
    registry.register(lmstudio)
    prefs = InMemoryPreferencesRepository()
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry, preferences=prefs,
        default_provider_id="lmstudio",
    )
    return provider, prefs, lmstudio


@pytest.mark.asyncio
async def test_group_reasoning_override_binds_resolved_adapter() -> None:
    provider, prefs, lmstudio = _wire_reasoning()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "lmstudio",
                "model_id": None,
                "reasoning": {"disable_reasoning": True},
            },
        },
    )

    model = await provider.resolve("post_turn")

    assert model is not lmstudio
    assert model.bound_overrides == ReasoningOverrides(disable_reasoning=True)


@pytest.mark.asyncio
async def test_reasoning_only_entry_applies_without_model_pin() -> None:
    """An entry may carry ONLY reasoning — the model keeps inheriting
    from active_model while the reasoning posture applies. This is the
    "same model, different effort per group" configuration."""
    provider, prefs, lmstudio = _wire_reasoning()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "shared"},
    )
    await prefs.set(
        "feature_model_groups",
        {
            "high_reasoning_gates": {
                "provider_id": None,
                "model_id": None,
                "reasoning": {"reasoning_effort": "high"},
            },
            "core_structured_memory": {
                "provider_id": None,
                "model_id": None,
                "reasoning": {"reasoning_effort": "medium"},
            },
        },
    )

    gate_model = await provider.resolve("schedule_plan")
    memory_model = await provider.resolve("post_turn")

    assert await provider.resolve_model_id("schedule_plan") == "shared"
    assert await provider.resolve_model_id("post_turn") == "shared"
    assert gate_model.bound_overrides == ReasoningOverrides(
        reasoning_effort="high",
    )
    assert memory_model.bound_overrides == ReasoningOverrides(
        reasoning_effort="medium",
    )


@pytest.mark.asyncio
async def test_feature_reasoning_wins_over_group_reasoning() -> None:
    provider, prefs, _ = _wire_reasoning()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": None,
                "model_id": None,
                "reasoning": {"reasoning_effort": "medium"},
            },
        },
    )
    await prefs.set(
        "feature_models",
        {
            "post_turn": {
                "provider_id": None,
                "model_id": None,
                "reasoning": {"disable_reasoning": True},
            },
        },
    )

    model = await provider.resolve("post_turn")

    assert model.bound_overrides == ReasoningOverrides(disable_reasoning=True)


@pytest.mark.asyncio
async def test_no_reasoning_override_returns_registry_instance() -> None:
    """Without any routing reasoning setting the resolver must hand back
    the registry singleton itself — zero per-call copies, behaviour
    identical to before this feature."""
    provider, prefs, lmstudio = _wire_reasoning()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "lmstudio",
                "model_id": "pinned",
            },
        },
    )

    assert await provider.resolve("post_turn") is lmstudio
    assert await provider.resolve("chat") is lmstudio


@pytest.mark.asyncio
async def test_reasoning_override_skips_adapters_without_binding() -> None:
    """Adapters that don't expose ``with_reasoning_overrides`` (fake,
    cloud gateway model) pass through unchanged."""
    provider, prefs, lmstudio, _ = _wire()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "lmstudio",
                "model_id": None,
                "reasoning": {"reasoning_effort": "high"},
            },
        },
    )

    assert await provider.resolve("post_turn") is lmstudio


@pytest.mark.asyncio
async def test_unknown_feature_key_skips_reasoning_override() -> None:
    provider, prefs, lmstudio = _wire_reasoning()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": None,
                "model_id": None,
                "reasoning": {"reasoning_effort": "high"},
            },
        },
    )

    assert await provider.resolve("not_a_feature") is lmstudio
    assert await provider.resolve() is lmstudio


@pytest.mark.asyncio
async def test_nsfw_mode_skips_routing_reasoning_override() -> None:
    """NSFW mode reroutes the whole call to a dedicated target; a group
    reasoning posture written for the normal route must not bind onto
    the NSFW adapter."""
    registry = InMemoryChatModelRegistry(default_provider_id="lmstudio")
    lmstudio = _ReasoningCapableModel("lmstudio", reply='{"memories": []}')
    nsfw_target = _ReasoningCapableModel("community", reply="ok")
    registry.register(lmstudio)
    registry.register(nsfw_target)
    prefs = InMemoryPreferencesRepository()
    nsfw = NsfwModeService(preferences=prefs, ttl_seconds=60)
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=prefs,
        default_provider_id="lmstudio",
        nsfw_mode_service=nsfw,
    )
    await nsfw.set_global_target(
        llm_provider_id="community",
        llm_model_id="community-nsfw",
        image_profile_id="anime_nsfw",
    )
    await nsfw.enable(user_id="alice")
    await prefs.set(
        "feature_model_groups",
        {
            "player_facing_voice": {
                "provider_id": None,
                "model_id": None,
                "reasoning": {"reasoning_effort": "high"},
            },
        },
    )

    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        user_id="alice",
    )
    model = await provider.resolve("chat", character=character)

    assert model is nsfw_target
    assert model.bound_overrides is None


# ---- routing-level vision overrides -----------------------------------


class _VisionCapableModel(_RecordingModel):
    """Recording model that also supports routing-level vision binding,
    mimicking the real openai_compatible / anthropic adapters."""

    def __init__(
        self,
        provider_id: str,
        *,
        reply: str = "{}",
        supports_vision: bool = True,
    ) -> None:
        super().__init__(provider_id, reply=reply)
        self.supports_vision = supports_vision

    def with_supports_vision(self, value: bool) -> "_VisionCapableModel":
        clone = copy.copy(self)
        clone.supports_vision = value
        return clone


def _wire_vision(*, base_supports_vision: bool = True) -> tuple[
    PreferenceBackedActiveLLMProvider,
    InMemoryPreferencesRepository,
    _VisionCapableModel,
]:
    registry = InMemoryChatModelRegistry(default_provider_id="lmstudio")
    lmstudio = _VisionCapableModel(
        "lmstudio", reply='{"memories": []}',
        supports_vision=base_supports_vision,
    )
    registry.register(lmstudio)
    prefs = InMemoryPreferencesRepository()
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry, preferences=prefs,
        default_provider_id="lmstudio",
    )
    return provider, prefs, lmstudio


@pytest.mark.asyncio
async def test_feature_vision_override_binds_clone_and_leaves_base() -> None:
    provider, prefs, lmstudio = _wire_vision()  # base supports_vision=True
    await prefs.set(
        "feature_models",
        {
            "post_turn": {
                "provider_id": "lmstudio",
                "model_id": None,
                "supports_vision": False,
            },
        },
    )

    model = await provider.resolve("post_turn")

    assert model is not lmstudio
    assert model.supports_vision is False
    # Registry-registered base singleton keeps its original flag.
    assert lmstudio.supports_vision is True


@pytest.mark.asyncio
async def test_group_vision_override_binds_clone() -> None:
    provider, prefs, lmstudio = _wire_vision()
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": "lmstudio",
                "model_id": None,
                "supports_vision": False,
            },
        },
    )

    model = await provider.resolve("post_turn")

    assert model.supports_vision is False
    assert lmstudio.supports_vision is True


@pytest.mark.asyncio
async def test_active_model_vision_override_binds_without_feature_key() -> None:
    """A vision pin on the primary active_model must bind even for plain
    resolution with no feature_key — the aggregator-connection case."""
    provider, prefs, lmstudio = _wire_vision()
    await prefs.set(
        "active_model",
        {"provider_id": "lmstudio", "model_id": "m", "supports_vision": False},
    )

    model = await provider.resolve()

    assert model.supports_vision is False
    assert lmstudio.supports_vision is True


@pytest.mark.asyncio
async def test_feature_vision_wins_over_group_and_active() -> None:
    provider, prefs, lmstudio = _wire_vision()
    await prefs.set(
        "active_model",
        {"provider_id": "lmstudio", "model_id": "m", "supports_vision": True},
    )
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": None, "model_id": None,
                "supports_vision": True,
            },
        },
    )
    await prefs.set(
        "feature_models",
        {
            "post_turn": {
                "provider_id": None, "model_id": None,
                "supports_vision": False,
            },
        },
    )

    # Only the feature layer pins False; group/active pin True. False wins.
    model = await provider.resolve("post_turn")
    assert model.supports_vision is False


@pytest.mark.asyncio
async def test_group_vision_wins_over_active_model() -> None:
    provider, prefs, lmstudio = _wire_vision()
    await prefs.set(
        "active_model",
        {"provider_id": "lmstudio", "model_id": "m", "supports_vision": True},
    )
    await prefs.set(
        "feature_model_groups",
        {
            "core_structured_memory": {
                "provider_id": None, "model_id": None,
                "supports_vision": False,
            },
        },
    )

    model = await provider.resolve("post_turn")
    assert model.supports_vision is False


def _vision_character(
    *, feature_models: list[FeatureModelOverride] | None = None,
) -> Character:
    return Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        feature_models=feature_models or [],
    )


@pytest.mark.asyncio
async def test_global_vision_does_not_bleed_onto_character_pinned_model() -> None:
    """The per-character FeatureModelOverride is the HIGHEST model-
    supplying layer. When it pins the model for a feature, the global
    feature/group/active_model vision flags describe DIFFERENT models
    and must not bind onto the character-pinned adapter (repro: a
    character pinned to a vision model got bound supports_vision=False
    from the global text-only entry → silent image loss)."""
    provider, prefs, lmstudio = _wire_vision(base_supports_vision=True)
    await prefs.set(
        "feature_models",
        {
            "chat": {
                "provider_id": "lmstudio",
                "model_id": None,
                "supports_vision": False,
            },
        },
    )
    character = _vision_character(feature_models=[
        FeatureModelOverride(
            feature_key="chat",
            provider_id="lmstudio",
            model_id="char-pinned",
        ),
    ])

    model = await provider.resolve("chat", character=character)

    # No binding at all: the untouched registry singleton keeps its
    # connection-level flag.
    assert model is lmstudio
    assert model.supports_vision is True


@pytest.mark.asyncio
async def test_character_model_id_only_pin_suppresses_global_vision() -> None:
    """Same rule when the character pins only model_id (provider falls
    through): the capability-determining model came from the character
    layer, so lower-layer flags stay out."""
    provider, prefs, lmstudio = _wire_vision(base_supports_vision=True)
    await prefs.set(
        "feature_models",
        {
            "chat": {
                "provider_id": None,
                "model_id": None,
                "supports_vision": False,
            },
        },
    )
    character = _vision_character(feature_models=[
        FeatureModelOverride(
            feature_key="chat",
            provider_id=None,
            model_id="char-model-only",
        ),
    ])

    model = await provider.resolve("chat", character=character)

    assert model is lmstudio
    assert model.supports_vision is True


@pytest.mark.asyncio
async def test_character_without_pin_does_not_block_global_vision() -> None:
    """A character with no override for the feature does not supply the
    model — the global feature entry's vision flag still applies (no
    over-suppression)."""
    provider, prefs, lmstudio = _wire_vision(base_supports_vision=True)
    await prefs.set(
        "feature_models",
        {
            "chat": {
                "provider_id": None,
                "model_id": None,
                "supports_vision": False,
            },
        },
    )
    character = _vision_character(feature_models=[
        # Override for a DIFFERENT feature — must not block chat.
        FeatureModelOverride(
            feature_key="post_turn",
            provider_id="lmstudio",
            model_id="other-feature",
        ),
    ])

    model = await provider.resolve("chat", character=character)

    assert model.supports_vision is False
    assert lmstudio.supports_vision is True


@pytest.mark.asyncio
async def test_active_vision_does_not_bleed_onto_group_pinned_model() -> None:
    """Cross-layer bleed regression (owner-config case): the group pins
    the model (e.g. player_facing_voice → openrouter deepseek, text-only,
    NO vision flag on the entry) while active_model is a different vision
    pick carrying supports_vision=true. The active_model flag describes
    the ACTIVE model, not the group's — it must NOT bind onto the
    group-pinned adapter, or images get attached to deepseek again."""
    provider, prefs, lmstudio = _wire_vision(base_supports_vision=False)
    await prefs.set(
        "active_model",
        {"provider_id": "lmstudio", "model_id": "m", "supports_vision": True},
    )
    await prefs.set(
        "feature_model_groups",
        {
            "player_facing_voice": {
                "provider_id": "lmstudio",
                "model_id": "group-pinned",
                # no supports_vision — inherit the connection flag.
            },
        },
    )

    model = await provider.resolve("chat")

    # No binding at all: the registry singleton comes back with its
    # connection-level flag intact.
    assert model is lmstudio
    assert model.supports_vision is False


@pytest.mark.asyncio
async def test_active_vision_does_not_bleed_onto_feature_pinned_model() -> None:
    """Same rule at the feature layer: an entry pinning only model_id
    (no vision flag) supplies the model, so the active_model vision flag
    must not ride along."""
    provider, prefs, lmstudio = _wire_vision(base_supports_vision=False)
    await prefs.set(
        "active_model",
        {"provider_id": "lmstudio", "model_id": "m", "supports_vision": True},
    )
    await prefs.set(
        "feature_models",
        {
            "post_turn": {
                "provider_id": None,
                "model_id": "feature-pinned",
            },
        },
    )

    model = await provider.resolve("post_turn")

    assert model is lmstudio
    assert model.supports_vision is False


@pytest.mark.asyncio
async def test_group_vision_does_not_bleed_onto_feature_pinned_model() -> None:
    """Same bleed rule one layer up: the group's vision flag describes
    the GROUP's pinned model. When the feature entry pins the model
    (e.g. novelty_gate → openrouter deepseek-flash, text-only, no flag)
    the group's supports_vision=true (written for the group's own vision
    pick) must NOT ride onto the feature-pinned adapter."""
    provider, prefs, lmstudio = _wire_vision(base_supports_vision=False)
    # novelty_gate's own group is critic_review (FEATURE_TO_GROUP).
    await prefs.set(
        "feature_model_groups",
        {
            "critic_review": {
                "provider_id": "lmstudio",
                "model_id": "group-vision-model",
                "supports_vision": True,
            },
        },
    )
    await prefs.set(
        "feature_models",
        {
            "novelty_gate": {
                "provider_id": "lmstudio",
                "model_id": "feature-text-only",
                # no supports_vision — inherit the connection flag.
            },
        },
    )

    model = await provider.resolve("novelty_gate")

    # No binding: the feature layer supplied the model, so the group's
    # flag (describing a different model) stays out.
    assert model is lmstudio
    assert model.supports_vision is False


@pytest.mark.asyncio
async def test_metadata_only_feature_entry_lets_group_vision_apply() -> None:
    """A metadata-only feature entry (reasoning, no provider/model pin)
    does not supply the model — the group layer does, so the group's
    vision flag applies."""
    provider, prefs, lmstudio = _wire_vision(base_supports_vision=False)
    await prefs.set(
        "feature_models",
        {
            "novelty_gate": {
                "provider_id": None,
                "model_id": None,
                "reasoning": {"reasoning_effort": "low"},
            },
        },
    )
    await prefs.set(
        "feature_model_groups",
        {
            "critic_review": {
                "provider_id": "lmstudio",
                "model_id": "group-vision-model",
                "supports_vision": True,
            },
        },
    )

    model = await provider.resolve("novelty_gate")

    assert model.supports_vision is True
    assert lmstudio.supports_vision is False


@pytest.mark.asyncio
async def test_metadata_only_feature_entry_does_not_block_active_vision() -> None:
    """A feature entry carrying ONLY metadata (reasoning, no provider or
    model pin) does not supply the model — the route genuinely falls
    through to active_model, so the active_model vision flag applies."""
    provider, prefs, lmstudio = _wire_vision(base_supports_vision=False)
    await prefs.set(
        "active_model",
        {"provider_id": "lmstudio", "model_id": "m", "supports_vision": True},
    )
    await prefs.set(
        "feature_models",
        {
            "post_turn": {
                "provider_id": None,
                "model_id": None,
                "reasoning": {"reasoning_effort": "high"},
            },
        },
    )

    model = await provider.resolve("post_turn")

    assert model.supports_vision is True
    assert lmstudio.supports_vision is False


@pytest.mark.asyncio
async def test_vision_only_entry_applies_while_model_routing_falls_through() -> None:
    """An entry pinning ONLY supports_vision binds the flag while the
    model itself keeps inheriting from active_model."""
    provider, prefs, lmstudio = _wire_vision()
    await prefs.set(
        "active_model", {"provider_id": "lmstudio", "model_id": "shared"},
    )
    await prefs.set(
        "feature_models",
        {
            "post_turn": {
                "provider_id": None, "model_id": None,
                "supports_vision": False,
            },
        },
    )

    model = await provider.resolve("post_turn")

    assert model.supports_vision is False
    assert model.provider_id == "lmstudio"
    # Model routing still falls through to the active_model pick.
    assert await provider.resolve_model_id("post_turn") == "shared"


@pytest.mark.asyncio
async def test_malformed_vision_value_ignored() -> None:
    provider, prefs, lmstudio = _wire_vision()  # base True
    await prefs.set(
        "feature_models",
        {
            "post_turn": {
                "provider_id": "lmstudio",
                "model_id": "m",
                "supports_vision": "yes",
            },
        },
    )

    # Malformed → no binding → registry singleton returned unchanged.
    assert await provider.resolve("post_turn") is lmstudio


@pytest.mark.asyncio
async def test_nsfw_active_target_skips_vision_override() -> None:
    registry = InMemoryChatModelRegistry(default_provider_id="lmstudio")
    lmstudio = _VisionCapableModel(
        "lmstudio", reply='{"memories": []}', supports_vision=True,
    )
    nsfw_target = _VisionCapableModel(
        "community", reply="ok", supports_vision=True,
    )
    registry.register(lmstudio)
    registry.register(nsfw_target)
    prefs = InMemoryPreferencesRepository()
    nsfw = NsfwModeService(preferences=prefs, ttl_seconds=60)
    provider = PreferenceBackedActiveLLMProvider(
        registry=registry, preferences=prefs,
        default_provider_id="lmstudio", nsfw_mode_service=nsfw,
    )
    await nsfw.set_global_target(
        llm_provider_id="community", llm_model_id="community-nsfw",
        image_profile_id="anime_nsfw",
    )
    await nsfw.enable(user_id="alice")
    await prefs.set(
        "feature_models",
        {
            "chat": {
                "provider_id": None, "model_id": None,
                "supports_vision": False,
            },
        },
    )
    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        user_id="alice",
    )

    model = await provider.resolve("chat", character=character)

    # NSFW target reached, vision override written for the normal route
    # NOT applied to the hijacked target.
    assert model is nsfw_target
    assert model.supports_vision is True


@pytest.mark.asyncio
async def test_vision_override_skips_adapters_without_binding() -> None:
    """Adapters that don't expose ``with_supports_vision`` (fake, cloud
    gateway) pass through unchanged."""
    provider, prefs, lmstudio, _ = _wire()  # _RecordingModel: no binder
    await prefs.set(
        "feature_models",
        {
            "post_turn": {
                "provider_id": "lmstudio", "model_id": None,
                "supports_vision": False,
            },
        },
    )

    assert await provider.resolve("post_turn") is lmstudio
