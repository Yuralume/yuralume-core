"""Core feature-key manifest — the contract the Cloud control-plane validates against.

The control-plane (``services/user``) owns runtime routing config: it stores
``feature_routes`` rows keyed by ``(capability, feature_key)``. ``feature_key`` is
a free string in the DB, but the *vocabulary* originates here in Core — Core is
the only thing that actually emits ``chat``, ``character_draft``, ``post_turn`` …
to the Gateway.

To stop a typo'd or unregistered key from silently falling through resolution,
Core publishes this manifest (``capability -> [feature_key]`` plus a content
hash) as a versioned contract artifact at ``contracts/feature-key-manifest.json``
(repo root). The control-plane bundles a copy and validates every
``feature_routes.feature_key`` against it, and a drift guard test
(:mod:`tests.unit.test_feature_key_manifest`) fails when Core gains a routable
feature key without regenerating the artifact.

Ownership: **Core owns the manifest; the control-plane consumes it as contract.**
Only routable feature keys belong here — keys that map to an actual Gateway
capability route (LLM / image / video / TTS). Usage-attribution-only labels
(``character_portrait``, ``feed_image``, ``auxiliary_llm`` …) are intentionally
excluded because they never select a route.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from kokoro_link.application.services.feature_keys import (
    GLOBAL_FEATURE_KEYS,
    IMAGE_FEATURE_KEYS,
    VIDEO_FEATURE_KEYS,
)

# Bump when the manifest *shape* changes in a breaking way (new top-level field,
# renamed capability). Additive feature-key changes are tracked by content_hash,
# not by this number.
MANIFEST_VERSION = 1

# Capabilities the Cloud Gateway actually routes today. ``embedding`` / ``search``
# are intentionally absent: the plan classifies them as net-new routing surface,
# not part of the config migration, so they must not appear as if they already
# route through the Gateway.
CAPABILITY_LLM = "llm"
CAPABILITY_IMAGE = "image"
CAPABILITY_VIDEO = "video"
CAPABILITY_TTS = "tts"

# The single routable TTS feature key. ``tts_translate`` is an *LLM* feature
# (dubbing translation before synthesis) and already lives in GLOBAL_FEATURE_KEYS,
# so it is not repeated here.
TTS_FEATURE_KEYS: tuple[str, ...] = ("tts_synthesis",)


@dataclass(frozen=True, slots=True)
class FeatureKeyManifest:
    """Immutable view of the routable feature-key vocabulary by capability."""

    manifest_version: int
    capabilities: dict[str, tuple[str, ...]]

    def feature_keys_for(self, capability: str) -> tuple[str, ...]:
        return self.capabilities.get(capability, ())

    def contains(self, capability: str, feature_key: str) -> bool:
        return feature_key in self.capabilities.get(capability, ())

    @property
    def content_hash(self) -> str:
        return _content_hash(self.capabilities)

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "content_hash": self.content_hash,
            "capabilities": {
                capability: list(keys)
                for capability, keys in self.capabilities.items()
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"


def build_feature_key_manifest() -> FeatureKeyManifest:
    """Assemble the manifest from the canonical Core feature-key registry."""

    capabilities: dict[str, tuple[str, ...]] = {
        CAPABILITY_LLM: _sorted_unique(GLOBAL_FEATURE_KEYS),
        CAPABILITY_IMAGE: _sorted_unique(IMAGE_FEATURE_KEYS),
        CAPABILITY_VIDEO: _sorted_unique(VIDEO_FEATURE_KEYS),
        CAPABILITY_TTS: _sorted_unique(TTS_FEATURE_KEYS),
    }
    return FeatureKeyManifest(
        manifest_version=MANIFEST_VERSION,
        capabilities=capabilities,
    )


def _sorted_unique(keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(keys)))


def _content_hash(capabilities: dict[str, tuple[str, ...]]) -> str:
    canonical = {
        capability: sorted(keys) for capability, keys in capabilities.items()
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
