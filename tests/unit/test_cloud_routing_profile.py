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
