"""Tiny stub of :class:`ActiveImageProviderPort` for unit tests.

Wraps a single :class:`ImageProviderPort` instance and ignores
``feature_key`` / ``character`` — most tests in this suite only care
that the active-image-provider port forwards to *some* concrete
generator. Letting each test allocate its own preference-backed
provider would mean wiring an in-memory PreferencesRepository, a
profile registry, and a stale-pref code path just to verify that
:class:`CharacterImageService` writes bytes to disk.
"""

from __future__ import annotations

from typing import Any

from kokoro_link.contracts.image_provider import ImageProviderPort
from kokoro_link.domain.entities.character import Character


class StaticActiveImageProvider:
    """Always resolves to the same ``ImageProviderPort``.

    Mirrors the duck-typed surface of :class:`ActiveImageProviderPort`
    so call sites accept it without changes. Exposes the wrapped
    provider on ``provider`` for tests that need to assert on
    captured arguments."""

    def __init__(self, provider: ImageProviderPort | None) -> None:
        self.provider = provider

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> ImageProviderPort | None:
        return self.provider

    async def resolve_profile_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> str | None:
        return "stub" if self.provider is not None else None
