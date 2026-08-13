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
    CAPABILITY_SEARCH,
    CAPABILITY_TTS,
    CAPABILITY_VIDEO,
    build_feature_key_manifest,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_GROUP_DESCRIPTIONS,
    FEATURE_GROUP_LABELS,
    FEATURE_GROUP_MEMBERS,
    GLOBAL_FEATURE_KEYS,
    IMAGE_FEATURE_KEYS,
    LLM_FEATURE_GROUP_KEYS,
    VIDEO_FEATURE_KEYS,
)

_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_SIBLING_CLOUD_ROOT = _WORKSPACE_ROOT / "yuralume-cloud"
_REPO_ROOT = (
    _SIBLING_CLOUD_ROOT
    if (
        (_SIBLING_CLOUD_ROOT / "services" / "user").is_dir()
        and (_SIBLING_CLOUD_ROOT / "admin-console").is_dir()
    )
    else _WORKSPACE_ROOT
)
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
    assert manifest.feature_keys_for(CAPABILITY_SEARCH) == ("web_search",)


def test_manifest_excludes_unknown_capabilities() -> None:
    manifest = build_feature_key_manifest()
    # ``embedding`` reaches the Gateway but has no ``feature_routes`` knob: its
    # preset comes from the deployment default, so publishing a routable key for
    # it would advertise a control-plane surface that does not exist. ``search``
    # does have one (Gateway POST /v1/search + search_feature_presets), so it is
    # a real capability here.
    assert "embedding" not in manifest.capabilities
    assert manifest.capabilities["search"] == ("web_search",)


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
    assert set(data) == {
        "manifest_version",
        "content_hash",
        "capabilities",
        "groups",
    }
    assert data["manifest_version"] == 1
    assert set(data["capabilities"]) == {
        CAPABILITY_LLM,
        CAPABILITY_IMAGE,
        CAPABILITY_VIDEO,
        CAPABILITY_TTS,
        CAPABILITY_SEARCH,
    }
    # Only the llm capability carries routing groups today.
    assert set(data["groups"]) == {CAPABILITY_LLM}


def test_groups_cover_declared_llm_group_keys_in_order() -> None:
    manifest = build_feature_key_manifest()
    assert manifest.group_keys_for(CAPABILITY_LLM) == LLM_FEATURE_GROUP_KEYS
    # Capabilities without groups do not appear in the groups map.
    assert manifest.group_keys_for(CAPABILITY_IMAGE) == ()
    assert manifest.group_keys_for(CAPABILITY_VIDEO) == ()
    assert manifest.group_keys_for(CAPABILITY_TTS) == ()
    assert manifest.group_keys_for(CAPABILITY_SEARCH) == ()


def test_each_group_is_labelled_described_and_scoped_to_llm_keys() -> None:
    manifest = build_feature_key_manifest()
    llm_keys = set(manifest.feature_keys_for(CAPABILITY_LLM))
    for group in manifest.groups[CAPABILITY_LLM]:
        assert group.label == FEATURE_GROUP_LABELS[group.key]
        assert group.label.strip()
        assert group.description == FEATURE_GROUP_DESCRIPTIONS[group.key]
        assert group.description.strip()
        assert group.members, f"group {group.key} has no members"
        assert set(group.members) <= llm_keys
        # Members preserve the curated declaration order (dedup, no sort).
        assert group.members == tuple(
            dict.fromkeys(FEATURE_GROUP_MEMBERS[group.key])
        )
        assert manifest.group_members(CAPABILITY_LLM, group.key) == group.members


def test_group_keys_are_disjoint_from_feature_keys() -> None:
    manifest = build_feature_key_manifest()
    group_keys = set(manifest.group_keys_for(CAPABILITY_LLM))
    feature_keys = set(manifest.feature_keys_for(CAPABILITY_LLM))
    # match_kind disambiguation at the routing layer relies on this.
    assert group_keys.isdisjoint(feature_keys)


def test_content_hash_covers_groups() -> None:
    data = json.loads(_CANONICAL.read_text(encoding="utf-8"))
    assert "groups" in data
    assert data["content_hash"] == build_feature_key_manifest().content_hash
