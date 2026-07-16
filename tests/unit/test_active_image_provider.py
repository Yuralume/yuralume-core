"""Unit tests for :class:`PreferenceBackedActiveImageProvider`.

The resolver is the load-bearing piece of per-feature / per-character
image routing — these tests pin the fallback chain so a regression
can't quietly route every character through the same profile.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.active_image_provider import (
    PreferenceBackedActiveImageProvider,
)
from kokoro_link.application.services.nsfw_mode import NsfwModeService
from kokoro_link.application.services.feature_keys import (
    FEATURE_IMAGE_CHAT_TOOL, FEATURE_IMAGE_PORTRAIT,
)
from kokoro_link.contracts.image_profile import (
    ComfyProfileConfig, ExternalImageApiProfileConfig,
    FeatureImageProfileOverride, ImageProfile,
    OpenAIProfileConfig,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.image.profile_registry import (
    ImageProfileRegistry,
)
from kokoro_link.infrastructure.image.gemini_provider import GeminiImageProvider
from kokoro_link.infrastructure.image.xai_provider import XAIImageProvider
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)


def _character(
    *,
    feature_image_profiles: tuple[FeatureImageProfileOverride, ...] = (),
) -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
    )
    return Character(
        id="c1", name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], state=state,
        feature_image_profiles=feature_image_profiles,
    )


def _two_profile_registry() -> ImageProfileRegistry:
    anime = ImageProfile(
        id="anime_local", label="Anime", kind="comfyui",
        comfyui=ComfyProfileConfig(
            server="127.0.0.1:8188",
            checkpoint="anime.safetensors",
        ),
    )
    openai = ImageProfile(
        id="openai_hi", label="OpenAI hi-q", kind="openai",
        openai=OpenAIProfileConfig(
            api_key="sk-test", quality="high",
        ),
    )
    return ImageProfileRegistry([anime, openai])


async def _set_nsfw_target(
    nsfw: NsfwModeService,
    *,
    image_profile_id: str = "openai_hi",
) -> None:
    await nsfw.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id=image_profile_id,
    )


@pytest.mark.asyncio
async def test_falls_back_to_first_profile_when_nothing_pinned() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    provider = PreferenceBackedActiveImageProvider(
        registry=registry, preferences=prefs,
    )

    profile_id = await provider.resolve_profile_id(
        FEATURE_IMAGE_PORTRAIT, character=_character(),
    )
    assert profile_id == "anime_local"


@pytest.mark.asyncio
async def test_global_active_profile_wins_over_first() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    await prefs.set("active_image_profile", {"profile_id": "openai_hi"})
    provider = PreferenceBackedActiveImageProvider(
        registry=registry, preferences=prefs,
    )

    profile_id = await provider.resolve_profile_id(
        FEATURE_IMAGE_PORTRAIT, character=_character(),
    )
    assert profile_id == "openai_hi"


@pytest.mark.asyncio
async def test_per_feature_global_override_wins_over_active() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    await prefs.set("active_image_profile", {"profile_id": "anime_local"})
    await prefs.set("image_feature_profiles", {
        "image_portrait": {"profile_id": "openai_hi"},
    })
    provider = PreferenceBackedActiveImageProvider(
        registry=registry, preferences=prefs,
    )

    assert await provider.resolve_profile_id(
        FEATURE_IMAGE_PORTRAIT, character=_character(),
    ) == "openai_hi"
    # Different feature key falls through to the global active pick.
    assert await provider.resolve_profile_id(
        FEATURE_IMAGE_CHAT_TOOL, character=_character(),
    ) == "anime_local"


@pytest.mark.asyncio
async def test_character_override_wins_over_everything() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    await prefs.set("active_image_profile", {"profile_id": "anime_local"})
    await prefs.set("image_feature_profiles", {
        "image_portrait": {"profile_id": "anime_local"},
    })
    provider = PreferenceBackedActiveImageProvider(
        registry=registry, preferences=prefs,
    )
    char = _character(feature_image_profiles=(
        FeatureImageProfileOverride(
            feature_key=FEATURE_IMAGE_PORTRAIT, profile_id="openai_hi",
        ),
    ))

    assert await provider.resolve_profile_id(
        FEATURE_IMAGE_PORTRAIT, character=char,
    ) == "openai_hi"


@pytest.mark.asyncio
async def test_nsfw_mode_overrides_character_and_global_image_routes() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    nsfw = NsfwModeService(preferences=prefs, ttl_seconds=60)
    await prefs.set("active_image_profile", {"profile_id": "anime_local"})
    await _set_nsfw_target(nsfw)
    await nsfw.enable(user_id="alice")
    provider = PreferenceBackedActiveImageProvider(
        registry=registry,
        preferences=prefs,
        nsfw_mode_service=nsfw,
    )
    char = Character(
        id="c1", name="Yui", summary="", user_id="alice",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
        ),
        feature_image_profiles=(
            FeatureImageProfileOverride(
                feature_key=FEATURE_IMAGE_PORTRAIT,
                profile_id="anime_local",
            ),
        ),
    )

    assert await provider.resolve_profile_id(
        FEATURE_IMAGE_PORTRAIT, character=char,
    ) == "openai_hi"


@pytest.mark.asyncio
async def test_expired_nsfw_image_mode_falls_back_to_normal_routing() -> None:
    from kokoro_link.application.services.scoped_preferences import (
        user_preference_key,
    )

    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    nsfw = NsfwModeService(preferences=prefs, ttl_seconds=60)
    await _set_nsfw_target(nsfw)
    await prefs.set(
        user_preference_key("alice", "nsfw_mode"),
        {
            "active": True,
            "last_activity_at": "2020-01-01T00:00:00+00:00",
        },
    )
    await prefs.set("active_image_profile", {"profile_id": "anime_local"})
    provider = PreferenceBackedActiveImageProvider(
        registry=registry,
        preferences=prefs,
        nsfw_mode_service=nsfw,
    )
    char = Character(
        id="c1", name="Yui", summary="", user_id="alice",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
        ),
    )

    assert await provider.resolve_profile_id(
        FEATURE_IMAGE_PORTRAIT, character=char,
    ) == "anime_local"


@pytest.mark.asyncio
async def test_stale_nsfw_image_profile_refuses_runtime_fallback() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    nsfw = NsfwModeService(preferences=prefs, ttl_seconds=60)
    await prefs.set("active_image_profile", {"profile_id": "anime_local"})
    await _set_nsfw_target(nsfw, image_profile_id="ghost_profile")
    await nsfw.enable(user_id="alice")
    provider = PreferenceBackedActiveImageProvider(
        registry=registry,
        preferences=prefs,
        nsfw_mode_service=nsfw,
    )
    char = Character(
        id="c1", name="Yui", summary="", user_id="alice",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
        ),
    )

    assert await provider.resolve_profile_id(
        FEATURE_IMAGE_PORTRAIT, character=char,
    ) is None
    assert await provider.resolve(
        FEATURE_IMAGE_PORTRAIT, character=char,
    ) is None


@pytest.mark.asyncio
async def test_stale_character_override_falls_through() -> None:
    """Character points at a profile the registry no longer knows — the
    resolver must skip the pin and use the next layer down, not blow up
    or return None."""
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    await prefs.set("active_image_profile", {"profile_id": "anime_local"})
    provider = PreferenceBackedActiveImageProvider(
        registry=registry, preferences=prefs,
    )
    char = _character(feature_image_profiles=(
        FeatureImageProfileOverride(
            feature_key=FEATURE_IMAGE_PORTRAIT, profile_id="ghost_profile",
        ),
    ))

    assert await provider.resolve_profile_id(
        FEATURE_IMAGE_PORTRAIT, character=char,
    ) == "anime_local"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_profiles_registered() -> None:
    registry = ImageProfileRegistry([])
    prefs = InMemoryPreferencesRepository()
    provider = PreferenceBackedActiveImageProvider(
        registry=registry, preferences=prefs,
    )
    assert await provider.resolve(
        FEATURE_IMAGE_PORTRAIT, character=_character(),
    ) is None
    assert await provider.resolve_profile_id() is None


@pytest.mark.asyncio
async def test_resolve_returns_built_provider() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    provider = PreferenceBackedActiveImageProvider(
        registry=registry, preferences=prefs,
    )

    built = await provider.resolve(
        FEATURE_IMAGE_PORTRAIT, character=_character(),
    )
    assert built is not None
    # Cached: same call returns the same instance.
    again = await provider.resolve(
        FEATURE_IMAGE_PORTRAIT, character=_character(),
    )
    assert again is built


def test_external_api_profile_dispatches_to_xai_native_provider() -> None:
    registry = ImageProfileRegistry([
        ImageProfile(
            id="grok",
            label="Grok",
            kind="external_api",
            api=ExternalImageApiProfileConfig(
                base_url="https://api.x.ai/v1",
                api_key="xai-key",
                model="grok-imagine-image-quality",
                provider="xai",
            ),
        ),
    ])

    assert isinstance(registry.resolve("grok"), XAIImageProvider)


def test_external_api_profile_dispatches_to_gemini_native_provider() -> None:
    registry = ImageProfileRegistry([
        ImageProfile(
            id="nano",
            label="Nano Banana",
            kind="external_api",
            api=ExternalImageApiProfileConfig(
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key="gemini-key",
                model="gemini-2.5-flash-image",
                provider="gemini",
            ),
        ),
    ])

    assert isinstance(registry.resolve("nano"), GeminiImageProvider)
