"""Unit tests for :class:`PreferenceBackedActiveVideoProvider`.

Pins the same four-layer fallback chain (character → global feature
→ global active → first registered) that the image-side resolver
guarantees. A regression here would silently route every character
through the wrong video backend."""

from __future__ import annotations

import pytest

from kokoro_link.application.services.active_video_provider import (
    PreferenceBackedActiveVideoProvider,
)
from kokoro_link.application.services.feature_keys import FEATURE_VIDEO_FEED
from kokoro_link.contracts.video_profile import (
    ExternalVideoApiProfileConfig, FeatureVideoProfileOverride, VideoProfile,
    WanVideoProfileConfig,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.video.profile_registry import (
    VideoProfileRegistry,
)
from kokoro_link.infrastructure.video.google_veo_provider import (
    GoogleVeoVideoProvider,
)


def _character(
    *,
    feature_video_profiles: tuple[FeatureVideoProfileOverride, ...] = (),
) -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
    )
    return Character(
        id="c1", name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], state=state,
        feature_video_profiles=feature_video_profiles,
    )


def _two_profile_registry() -> VideoProfileRegistry:
    a = VideoProfile(
        id="local", label="local", kind="comfyui_wan22",
        comfyui_wan22=WanVideoProfileConfig(server="127.0.0.1:8188"),
    )
    b = VideoProfile(
        id="alt", label="alt", kind="comfyui_wan22",
        comfyui_wan22=WanVideoProfileConfig(server="10.0.0.10:8188"),
    )
    return VideoProfileRegistry([a, b])


@pytest.mark.asyncio
async def test_falls_back_to_first_when_nothing_pinned() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    p = PreferenceBackedActiveVideoProvider(
        registry=registry, preferences=prefs,
    )
    assert await p.resolve_profile_id(
        FEATURE_VIDEO_FEED, character=_character(),
    ) == "local"


@pytest.mark.asyncio
async def test_character_override_wins() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    await prefs.set("active_video_profile", {"profile_id": "local"})
    p = PreferenceBackedActiveVideoProvider(
        registry=registry, preferences=prefs,
    )
    char = _character(feature_video_profiles=(
        FeatureVideoProfileOverride(
            feature_key=FEATURE_VIDEO_FEED, profile_id="alt",
        ),
    ))
    assert await p.resolve_profile_id(
        FEATURE_VIDEO_FEED, character=char,
    ) == "alt"


@pytest.mark.asyncio
async def test_per_feature_global_override_wins_over_active() -> None:
    registry = _two_profile_registry()
    prefs = InMemoryPreferencesRepository()
    await prefs.set("active_video_profile", {"profile_id": "local"})
    await prefs.set("video_feature_profiles", {
        "video_feed": {"profile_id": "alt"},
    })
    p = PreferenceBackedActiveVideoProvider(
        registry=registry, preferences=prefs,
    )
    assert await p.resolve_profile_id(
        FEATURE_VIDEO_FEED, character=_character(),
    ) == "alt"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_empty_registry() -> None:
    registry = VideoProfileRegistry([])
    prefs = InMemoryPreferencesRepository()
    p = PreferenceBackedActiveVideoProvider(
        registry=registry, preferences=prefs,
    )
    assert await p.resolve(
        FEATURE_VIDEO_FEED, character=_character(),
    ) is None
    assert await p.resolve_profile_id() is None


def test_external_api_profile_dispatches_to_google_veo_native_provider() -> None:
    registry = VideoProfileRegistry([
        VideoProfile(
            id="veo",
            label="Veo",
            kind="external_api",
            api=ExternalVideoApiProfileConfig(
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key="gemini-key",
                model="veo-3.1-generate-preview",
                provider="google_veo",
            ),
        ),
    ])

    assert isinstance(registry.resolve("veo"), GoogleVeoVideoProvider)
