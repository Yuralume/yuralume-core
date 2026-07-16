"""Preference-backed :class:`ActiveImageProviderPort` implementation.

Three layers of preference drive resolution, highest priority first:

  * Per-character ``character.feature_image_profile_for(key)`` — only
    consulted when the caller passes a ``character``. Lets operators
    pin one character to a different image profile than the rest of the
    app (anime vs realistic, fast vs polished, …).

  * Global ``image_feature_profiles`` — per-feature overrides written
    by the per-feature image profile picker. Shape::

        {
          "image_chat_tool": {"profile_id": "anime_local"},
          "image_portrait":  {"profile_id": "openai_polished"},
          ...
        }

  * Global ``active_image_profile`` — single picker. Shape::

        {"profile_id": "anime_local"}

Final fallback is the first profile registered in
:class:`ImageProfileRegistry`. Malformed entries / unknown profile ids
degrade to the next level so a stale preference doesn't take image
generation offline; the worst case is "wrong profile picked", which
the operator can fix in the UI.
"""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.contracts.active_image import ActiveImageProviderPort
from kokoro_link.contracts.image_provider import ImageProviderPort
from kokoro_link.contracts.repositories import PreferencesRepositoryPort
from kokoro_link.application.services.nsfw_mode import NsfwModeService
from kokoro_link.application.services.scoped_preferences import (
    get_preference_with_user_fallback,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.image.profile_registry import (
    ImageProfileRegistry,
)

_LOGGER = logging.getLogger(__name__)
_ACTIVE_KEY = "active_image_profile"
_FEATURE_KEY = "image_feature_profiles"


class PreferenceBackedActiveImageProvider(ActiveImageProviderPort):
    def __init__(
        self,
        *,
        registry: ImageProfileRegistry,
        preferences: PreferencesRepositoryPort,
        nsfw_mode_service: NsfwModeService | None = None,
    ) -> None:
        self._registry = registry
        self._preferences = preferences
        self._nsfw_mode_service = nsfw_mode_service

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> ImageProviderPort | None:
        profile_id = await self.resolve_profile_id(
            feature_key, character=character,
        )
        if profile_id is None:
            return None
        provider = self._registry.resolve(profile_id)
        if provider is not None:
            return provider
        # Stale preference (profile renamed/removed) — fall back to the
        # registry's first profile so generation keeps working until the
        # operator fixes the picker.
        _LOGGER.warning(
            "active image: profile %r not registered (feature=%s, "
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
        nsfw_profile_id = await self._read_nsfw_profile_id(character=character)
        if nsfw_profile_id is not None:
            if self._registry.get_profile(nsfw_profile_id) is None:
                _LOGGER.error(
                    "active image: NSFW mode profile %r is not registered; "
                    "refusing fallback",
                    nsfw_profile_id,
                )
                return None
            return nsfw_profile_id
        # Per-character override first.
        if feature_key and character is not None:
            override = character.feature_image_profile_for(feature_key)
            if override is not None and override.profile_id:
                if self._registry.get_profile(override.profile_id) is not None:
                    return override.profile_id
                _LOGGER.warning(
                    "character %s pins image profile %r which is not "
                    "registered; falling through",
                    character.id, override.profile_id,
                )
        # Global per-feature override.
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
        # Global active.
        raw = await self._read_pref(_ACTIVE_KEY, character=character)
        if isinstance(raw, dict):
            value = raw.get("profile_id")
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                if self._registry.get_profile(candidate) is not None:
                    return candidate
        # Final fallback: first registered profile.
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
                "active image: preferences read failed for %r", key,
            )
            return None

    async def _read_nsfw_profile_id(
        self,
        *,
        character: Character | None,
    ) -> str | None:
        if self._nsfw_mode_service is None or character is None:
            return None
        target = await self._nsfw_mode_service.active_target(
            user_id=getattr(character, "user_id", None),
        )
        if target is None:
            return None
        return target.image_profile_id
