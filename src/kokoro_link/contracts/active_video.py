"""Resolve the currently active video provider at call time.

Mirror of :class:`ActiveImageProviderPort`. Lives as a separate port
so the per-feature key set and the routing surfaces don't bleed —
video features and image features may both pick from the same
character but resolve against different profile registries.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.contracts.video_provider import VideoProviderPort
from kokoro_link.domain.entities.character import Character


class ActiveVideoProviderPort(Protocol):
    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> VideoProviderPort | None:
        """Return the video provider currently routed for ``feature_key``
        (optionally narrowed by ``character``).

        ``None`` signals "no video generation available right now" —
        either nothing is configured or the resolved profile id points
        at a profile whose backend isn't reachable. Callers degrade
        (fall back to image, or text-only post)."""

    async def resolve_profile_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> str | None:
        """Return the profile id selected for ``feature_key`` or ``None``."""
