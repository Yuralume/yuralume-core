"""Drift guard for the Core feature-key manifest contract artifact.

These tests make "a new Core feature key cannot silently miss Cloud routing
coverage" mechanically enforceable: if a routable feature key is added to
``feature_keys.py`` without regenerating the on-disk artifacts, the manifest's
content hash changes and the byte-compare against the checked-in JSON fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kokoro_link.application.services.feature_key_manifest import (
    CAPABILITY_IMAGE,
    CAPABILITY_LLM,
    CAPABILITY_TTS,
    CAPABILITY_VIDEO,
    build_feature_key_manifest,
)
from kokoro_link.application.services.feature_keys import (
    GLOBAL_FEATURE_KEYS,
    IMAGE_FEATURE_KEYS,
    VIDEO_FEATURE_KEYS,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANONICAL = _REPO_ROOT / "contracts" / "feature-key-manifest.json"
_USER_COPY = (
    _REPO_ROOT
    / "services"
    / "user"
    / "src"
    / "main"
    / "resources"
    / "contracts"
    / "feature-key-manifest.json"
)


def test_manifest_covers_every_routable_registry_key() -> None:
    manifest = build_feature_key_manifest()
    assert set(manifest.feature_keys_for(CAPABILITY_LLM)) == set(GLOBAL_FEATURE_KEYS)
    assert set(manifest.feature_keys_for(CAPABILITY_IMAGE)) == set(IMAGE_FEATURE_KEYS)
    assert set(manifest.feature_keys_for(CAPABILITY_VIDEO)) == set(VIDEO_FEATURE_KEYS)
    assert manifest.feature_keys_for(CAPABILITY_TTS) == ("tts_synthesis",)


def test_manifest_excludes_unknown_capabilities() -> None:
    manifest = build_feature_key_manifest()
    # embedding / search are net-new routing surface and must not appear yet.
    assert "embedding" not in manifest.capabilities
    assert "search" not in manifest.capabilities


def test_content_hash_is_stable_and_namespaced() -> None:
    manifest = build_feature_key_manifest()
    assert manifest.content_hash.startswith("sha256:")
    # Deterministic across rebuilds.
    assert manifest.content_hash == build_feature_key_manifest().content_hash


@pytest.mark.parametrize("artifact", [_CANONICAL, _USER_COPY])
def test_on_disk_artifact_matches_registry(artifact: Path) -> None:
    assert artifact.exists(), (
        f"missing {artifact}; run scripts/export_feature_key_manifest.py"
    )
    expected = build_feature_key_manifest().to_json()
    actual = artifact.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{artifact} is stale; run scripts/export_feature_key_manifest.py"
    )


def test_on_disk_copies_agree() -> None:
    assert _CANONICAL.read_text(encoding="utf-8") == _USER_COPY.read_text(
        encoding="utf-8"
    )


def test_artifact_is_valid_json_with_expected_shape() -> None:
    data = json.loads(_CANONICAL.read_text(encoding="utf-8"))
    assert set(data) == {"manifest_version", "content_hash", "capabilities"}
    assert data["manifest_version"] == 1
    assert set(data["capabilities"]) == {
        CAPABILITY_LLM,
        CAPABILITY_IMAGE,
        CAPABILITY_VIDEO,
        CAPABILITY_TTS,
    }
