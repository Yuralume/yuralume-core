"""Ports and DTOs for chat prompt material digest."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from kokoro_link.domain.entities.character import Character


@dataclass(frozen=True, slots=True)
class PromptMaterialDigestContext:
    character_id: str
    operator_id: str
    emotion_events: tuple[str, ...] = ()
    self_reflections: tuple[str, ...] = ()
    story_events: tuple[str, ...] = ()
    story_arc: tuple[str, ...] = ()
    recent_feed_posts: tuple[str, ...] = ()
    source_language: str = ""
    content_tolerance: str = "frontier"


@dataclass(frozen=True, slots=True)
class PromptMaterialDigest:
    bullets: tuple[str, ...]
    digest_metadata: dict[str, Any] = field(default_factory=dict)


class PromptMaterialDigestPort(Protocol):
    async def digest(
        self,
        context: PromptMaterialDigestContext,
        *,
        character: Character | None = None,
    ) -> PromptMaterialDigest | None:
        """Return fact bullets for poetic prompt material, or ``None`` fail-soft."""
