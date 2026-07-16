"""Tests for external video profile loading."""

from __future__ import annotations

import json

from kokoro_link.bootstrap.video_profiles import load_video_profiles
from kokoro_link.contracts.video_profile import ExternalVideoApiProfileConfig


def test_external_video_api_profile_shape() -> None:
    raw = json.dumps([{
        "id": "veo3",
        "label": "Veo 3",
        "kind": "external_api",
        "api": {
            "base_url": "https://gateway.example/v1",
            "api_key": "token",
            "model": "veo3",
            "provider": "gateway",
            "timeout_seconds": 600,
        },
    }])
    profiles = load_video_profiles(raw_config=raw)
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.kind == "external_api"
    assert profile.api is not None
    assert profile.api.base_url == "https://gateway.example/v1"
    assert profile.api.model == "veo3"
    assert profile.api.provider == "gateway"
    assert profile.api.timeout_seconds == 600


def test_empty_raw_uses_simple_external_api_profile() -> None:
    default_api = ExternalVideoApiProfileConfig(
        base_url="https://gateway.example/v1",
        api_key="token",
        model="veo3",
        provider="gateway",
        timeout_seconds=900,
    )
    profiles = load_video_profiles(raw_config="", default_api=default_api)

    assert len(profiles) == 1
    assert profiles[0].kind == "external_api"
    assert profiles[0].api is not None
    assert profiles[0].api.model == "veo3"
    assert profiles[0].api.provider == "gateway"
