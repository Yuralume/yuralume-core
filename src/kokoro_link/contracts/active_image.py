"""Resolve the "currently active" image provider at call time.

Mirrors :class:`ActiveLLMProviderPort` but for image generation:
auxiliary surfaces (chat tool, portrait service, feed composer) hold
this port instead of a frozen ``ImageProviderPort`` so a mid-session
preference flip — or a per-character override edit — takes effect
without a process restart.

Fallback chain (highest priority first):

1. ``character.feature_image_profile_for(feature_key)`` — only consulted
   when ``character`` is supplied. Lets per-character pins shadow every
   global setting for that one feature (e.g. character A in realistic
   style, character B in anime style).
2. Global ``image_feature_profiles[feature_key]`` preference.
3. Global ``active_image_profile`` preference.
4. The first profile registered by the container, when nothing else
   resolves.

Returns ``None`` when no profile is available at all — callers handle
this the same way they handled "image provider not configured" before
(HTTP 503 from REST, apology in chat, text-only feed post).
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.contracts.image_provider import ImageProviderPort
from kokoro_link.domain.entities.character import Character


class ActiveImageProviderPort(Protocol):
    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> ImageProviderPort | None:
        """Return the image provider currently selected for
        ``feature_key`` (optionally narrowed by ``character``).

        ``None`` signals "no image generation available right now" —
        either nothing is configured or the resolved profile id points
        at a profile whose backend is offline (e.g. missing API key).
        Callers degrade gracefully (text-only / 503) rather than
        erroring the whole request.

        Never raises — auxiliary services are non-critical path.
        """

    async def resolve_profile_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> str | None:
        """Return the profile id selected for ``feature_key`` or
        ``None`` when nothing is configured. Used by routes and the
        admin UI to surface which profile is in effect for a given
        feature/character combination — distinct from :meth:`resolve`
        which returns a built provider."""
