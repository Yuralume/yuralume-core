from __future__ import annotations

from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfile


def test_from_payload_parses_core_profile_response() -> None:
    profile = CloudRoutingProfile.from_payload({
        "tenant_id": "demo",
        "account_id": "acct_1",
        "tier": "demo",
        "llm_feature_presets": {"chat": "demo-gb10-chat", "post_turn": "demo-gb10-post"},
        "image_feature_presets": {"image_portrait": "demo-image"},
        "video_feature_presets": {},
        "tts_voice_defaults": {},
        "strict_no_fallback": True,
        "disabled_features": ["video:video_feed", "tts:tts_synthesis"],
        "catalog_version": 5,
        "routing_policy_version": 42,
    })

    assert profile.preset_for("llm", "chat") == "demo-gb10-chat"
    assert profile.preset_for("image", "image_portrait") == "demo-image"
    assert profile.preset_for("llm", "unknown") is None
    assert profile.strict_no_fallback is True
    assert profile.is_disabled("video", "video_feed") is True
    assert profile.is_disabled("tts", "tts_synthesis") is True
    assert profile.is_disabled("llm", "chat") is False
    assert profile.catalog_version == 5
    assert profile.routing_policy_version == 42
    assert "catalog=5" in profile.source and "routing=42" in profile.source


def test_from_payload_tolerates_missing_fields() -> None:
    profile = CloudRoutingProfile.from_payload({})
    assert profile.llm_feature_presets == {}
    assert profile.strict_no_fallback is False
    assert profile.disabled_features == frozenset()
    assert profile.catalog_version == 0


def test_from_payload_parses_llm_preset_vision_map() -> None:
    profile = CloudRoutingProfile.from_payload({
        "llm_feature_presets": {"chat": "hosted-text-mini"},
        "llm_preset_vision": {
            "hosted-text-mini": False,
            "hosted-vision": True,
        },
    })

    assert profile.llm_preset_vision == {
        "hosted-text-mini": False,
        "hosted-vision": True,
    }


def test_supports_vision_for_is_three_state() -> None:
    profile = CloudRoutingProfile.from_payload({
        "llm_preset_vision": {"text-only": False, "vision-capable": True},
    })

    assert profile.supports_vision_for("text-only") is False
    assert profile.supports_vision_for("vision-capable") is True
    # Unlisted preset = the control plane never pinned it = inherit the
    # adapter default, which is NOT the same answer as "pinned False".
    assert profile.supports_vision_for("never-mentioned") is None


def test_missing_vision_map_pins_nothing() -> None:
    profile = CloudRoutingProfile.from_payload({})

    assert profile.llm_preset_vision == {}
    assert profile.supports_vision_for("anything") is None


def test_non_boolean_vision_entries_are_ignored_not_coerced() -> None:
    """Only a real JSON boolean is a pin.

    A stringified ``"false"`` or a ``0`` is an unexpected shape, and
    guessing its intent would silently flip a model's declared capability
    (the same reason ``parse_vision_override`` refuses coercion). Such an
    entry is dropped, which lands on the safe "unpinned" default rather
    than on a guessed pin.
    """
    profile = CloudRoutingProfile.from_payload({
        "llm_preset_vision": {
            "stringy-false": "false",
            "stringy-true": "true",
            "zero": 0,
            "one": 1,
            "nulled": None,
            "real-false": False,
        },
    })

    assert profile.llm_preset_vision == {"real-false": False}
    for preset in ("stringy-false", "stringy-true", "zero", "one", "nulled"):
        assert profile.supports_vision_for(preset) is None


def test_non_dict_vision_map_is_ignored() -> None:
    profile = CloudRoutingProfile.from_payload({"llm_preset_vision": ["nope"]})

    assert profile.llm_preset_vision == {}
