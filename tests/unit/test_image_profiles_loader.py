"""Tests for the ``KOKORO_IMAGE_PROFILES`` loader.

Covers the input shapes the env accepts (inline JSON, path to a
JSON file, empty → simple API profile), plus ``${VAR}`` interpolation
in string values so API keys can live in env instead of plaintext JSON.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kokoro_link.bootstrap.image_profiles import load_image_profiles
from kokoro_link.contracts.image_profile import (
    ExternalImageApiProfileConfig,
)


def test_inline_json_yields_profiles() -> None:
    raw = json.dumps([
        {
            "id": "anime_local",
            "label": "Anime",
            "kind": "comfyui",
            "comfyui": {
                "server": "127.0.0.1:8188",
                "checkpoint": "anime.safetensors",
                "workflow_file": "workflows/anime.json",
            },
        },
        {
            "id": "openai_hi",
            "kind": "openai",
            "openai": {"api_key": "sk-test", "quality": "high"},
        },
    ])

    profiles = load_image_profiles(raw_config=raw)
    assert [p.id for p in profiles] == ["anime_local", "openai_hi"]
    assert profiles[0].kind == "comfyui"
    assert profiles[0].comfyui.checkpoint == "anime.safetensors"
    assert profiles[1].kind == "openai"
    assert profiles[1].openai.quality == "high"
    # Missing label defaults to id.
    assert profiles[1].label == "openai_hi"


def test_env_var_interpolation_resolves_at_load_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_KEY", "sk-resolved")
    raw = json.dumps([{
        "id": "p",
        "kind": "openai",
        "openai": {"api_key": "${SECRET_KEY}"},
    }])
    profiles = load_image_profiles(raw_config=raw)
    assert profiles[0].openai.api_key == "sk-resolved"


def test_external_image_api_profile_shape() -> None:
    raw = json.dumps([{
        "id": "gpt-image2",
        "label": "GPT Image 2",
        "kind": "external_api",
        "api": {
            "base_url": "https://gateway.example/v1",
            "api_key": "token",
            "model": "gpt-image2",
            "provider": "gateway",
            "timeout_seconds": 45,
        },
    }])
    profiles = load_image_profiles(raw_config=raw)
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.kind == "external_api"
    assert profile.api is not None
    assert profile.api.base_url == "https://gateway.example/v1"
    assert profile.api.model == "gpt-image2"
    assert profile.api.provider == "gateway"
    assert profile.api.timeout_seconds == 45


def test_unresolved_interpolation_drops_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    raw = json.dumps([{
        "id": "p",
        "kind": "openai",
        "openai": {"api_key": "${MISSING_KEY}"},
    }])
    profiles = load_image_profiles(raw_config=raw)
    # API key resolves empty → profile rejected at parse time.
    assert profiles == []


def test_path_input_reads_json_file(tmp_path: Path) -> None:
    config_path = tmp_path / "profiles.json"
    config_path.write_text(json.dumps([{
        "id": "p", "kind": "comfyui",
        "comfyui": {"server": "x", "checkpoint": "c"},
    }]), encoding="utf-8")
    profiles = load_image_profiles(raw_config=str(config_path))
    assert len(profiles) == 1
    assert profiles[0].id == "p"


def test_empty_raw_uses_simple_external_api_profile() -> None:
    default_api = ExternalImageApiProfileConfig(
        base_url="https://gateway.example/v1",
        api_key="token",
        model="gpt-image2",
        provider="gateway",
        timeout_seconds=90,
    )
    profiles = load_image_profiles(raw_config="", default_api=default_api)
    assert len(profiles) == 1
    assert profiles[0].id == "default"
    assert profiles[0].kind == "external_api"
    assert profiles[0].api is not None
    assert profiles[0].api.model == "gpt-image2"
    assert profiles[0].api.provider == "gateway"


def test_empty_raw_no_legacy_returns_empty() -> None:
    assert load_image_profiles(raw_config="") == []


def test_malformed_entries_dropped_keeps_rest() -> None:
    raw = json.dumps([
        {"id": "ok", "kind": "comfyui",
         "comfyui": {"server": "s", "checkpoint": "c"}},
        {"kind": "comfyui", "comfyui": {"server": "s"}},          # missing id
        {"id": "x", "kind": "unknown"},                            # bad kind
        {"id": "y", "kind": "comfyui", "comfyui": {"server": ""}}, # blank server
        "not-a-dict",
    ])
    profiles = load_image_profiles(raw_config=raw)
    assert [p.id for p in profiles] == ["ok"]


def test_duplicate_ids_keep_first() -> None:
    raw = json.dumps([
        {"id": "p", "kind": "comfyui",
         "comfyui": {"server": "a", "checkpoint": "c1"}},
        {"id": "p", "kind": "comfyui",
         "comfyui": {"server": "b", "checkpoint": "c2"}},
    ])
    profiles = load_image_profiles(raw_config=raw)
    assert len(profiles) == 1
    assert profiles[0].comfyui.server == "a"
