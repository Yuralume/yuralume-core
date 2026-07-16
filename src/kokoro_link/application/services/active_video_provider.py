"""Preference-backed :class:`ActiveVideoProviderPort` implementation.

Mirror of :class:`PreferenceBackedActiveImageProvider`. Same three-
layer fallback chain (character → global per-feature → global active
→ first registered) so operators don't have to learn a different
mental model for the video side.
"""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.contracts.active_video import ActiveVideoProviderPort
from kokoro_link.contracts.repositories import PreferencesRepositoryPort
from kokoro_link.contracts.video_provider import VideoProviderPort
from kokoro_link.application.services.scoped_preferences import (
    get_preference_with_user_fallback,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.video.profile_registry import (
    VideoProfileRegistry,
)

_LOGGER = logging.getLogger(__name__)
_ACTIVE_KEY = "active_video_profile"
_FEATURE_KEY = "video_feature_profiles"


class PreferenceBackedActiveVideoProvider(ActiveVideoProviderPort):
    def __init__(
        self,
        *,
        registry: VideoProfileRegistry,
        preferences: PreferencesRepositoryPort,
    ) -> None:
        self._registry = registry
        self._preferences = preferences

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> VideoProviderPort | None:
        profile_id = await self.resolve_profile_id(
            feature_key, character=character,
        )
        if profile_id is None:
            return None
        provider = self._registry.resolve(profile_id)
        if provider is not None:
            return provider
        _LOGGER.warning(
            "active video: profile %r not registered (feature=%s, "
            "character=%s); falling back to first available profile",
            profile_id, feature_key,
            character.id if character is not None else None,
        )
        fallback_id = self._fallback_id()
        if fallback_id is None:
            return None
        return self._registry.resolve(fallback_id)

    async def resolve_profile_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> str | None:
        if feature_key and character is not None:
            override = character.feature_video_profile_for(feature_key)
            if override is not None and override.profile_id:
                if self._registry.get_profile(override.profile_id) is not None:
                    return override.profile_id
                _LOGGER.warning(
                    "character %s pins video profile %r which is not "
                    "registered; falling through",
                    character.id, override.profile_id,
                )
        if feature_key:
            entry = await self._read_feature_entry(
                feature_key, character=character,
            )
            if entry is not None:
                value = entry.get("profile_id")
                if isinstance(value, str) and value.strip():
                    candidate = value.strip()
                    if self._registry.get_profile(candidate) is not None:
                        return candidate
        raw = await self._read_pref(_ACTIVE_KEY, character=character)
        if isinstance(raw, dict):
            value = raw.get("profile_id")
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                if self._registry.get_profile(candidate) is not None:
                    return candidate
        return self._fallback_id()

    # ---- internals ----------------------------------------------------

    def _fallback_id(self) -> str | None:
        ids = self._registry.profile_ids
        return ids[0] if ids else None

    async def _read_feature_entry(
        self,
        feature_key: str,
        *,
        character: Character | None = None,
    ) -> dict[str, Any] | None:
        raw = await self._read_pref(_FEATURE_KEY, character=character)
        if not isinstance(raw, dict):
            return None
        entry = raw.get(feature_key)
        if isinstance(entry, dict):
            return entry
        return None

    async def _read_pref(
        self,
        key: str,
        *,
        character: Character | None = None,
    ) -> Any:
        try:
            user_id = getattr(character, "user_id", None) if character else None
            return await get_preference_with_user_fallback(
                self._preferences,
                key,
                user_id=user_id,
            )
        except Exception:
            _LOGGER.exception(
                "active video: preferences read failed for %r", key,
            )
            return None
