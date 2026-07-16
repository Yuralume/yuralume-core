"""Character feed post entity (動態牆).

A ``FeedPost`` is an Instagram-style entry the character autonomously
publishes during a ProactiveScheduler tick. It pairs a short narrative
text with an optional image and a provenance pointer back to whatever
domain object inspired it (a schedule activity, a story beat, a memory,
a world event, or a derived signal like "user has been silent").

**Design choices**

- Frozen dataclass + ``with_*`` mutators: matches the rest of the
  domain layer; easy to test and reason about.
- ``image_url`` is optional because image generation can fail
  (ComfyUI unreachable, timeout); the post still ships text-only
  rather than the whole tick failing.
- ``source`` is a ``FeedSource`` value object — the (kind, ref_id)
  tuple drives composer-time dedup so the same beat doesn't spawn
  two posts on a single day even if the tick fires twice.
- ``reactions_seen_at`` records the moment the character last "read"
  user reactions; Phase 2 will use it to build a delta of unseen
  likes/comments to feed into prompts. Phase 1 leaves it ``None``.
- ``reaction_summary`` is a denormalised snapshot of reaction counts
  so the API can render cards without touching the reactions table.
  Phase 1 keeps it at zero; Phase 2 updates it when reactions land.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Sentinel for tri-state ``with_fields`` updates: lets callers say
# "leave image_url alone" vs. "explicitly clear it" without overloading
# ``None`` for both meanings.
_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class FeedReactionSummary:
    """Denormalised reaction counts cached on the post row.

    Cheap to serve in list endpoints. Phase 2 keeps this in sync when
    reactions / comments are written; Phase 1 always emits zeros.
    """

    likes: int = 0
    comments: int = 0

    def __post_init__(self) -> None:
        if self.likes < 0:
            raise ValueError("FeedReactionSummary.likes must be >= 0")
        if self.comments < 0:
            raise ValueError("FeedReactionSummary.comments must be >= 0")


@dataclass(frozen=True, slots=True)
class FeedPost:
    """One feed entry for a single character.

    ``content_text`` is the post body — single paragraph, written from
    the character's first-person voice. ``image_url`` is the absolute
    URL the API returns; for ComfyUI-generated images it points at a
    file written under the uploads directory.
    """

    id: str
    character_id: str
    kind: FeedKind
    content_text: str
    source: FeedSource
    created_at: datetime = field(default_factory=_utcnow)
    image_url: str | None = None
    image_prompt: str | None = None
    """Positive prompt the composer fed to ComfyUI. Stored for
    debugging and for future regenerate-with-tweaks affordances; not
    surfaced to end users."""
    video_url: str | None = None
    """Absolute URL of the short clip when the composer picked
    ``media_kind=video`` and Wan2.2 generation succeeded. When set,
    the frontend prefers ``<video>`` over ``<img>``; ``image_url`` may
    still be populated as a fallback poster frame (currently unused,
    but reserved for that)."""
    video_prompt: str | None = None
    """Natural-language English prompt the composer fed to Wan2.2.
    Same purpose as ``image_prompt`` — debugging + future regenerate."""
    reactions_seen_at: datetime | None = None
    reactions: FeedReactionSummary = field(default_factory=FeedReactionSummary)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("FeedPost.id must be non-empty")
        if not self.character_id:
            raise ValueError("FeedPost.character_id must be non-empty")
        if not isinstance(self.kind, FeedKind):
            raise TypeError("FeedPost.kind must be a FeedKind")
        if not isinstance(self.source, FeedSource):
            raise TypeError("FeedPost.source must be a FeedSource")
        if not isinstance(self.reactions, FeedReactionSummary):
            raise TypeError(
                "FeedPost.reactions must be a FeedReactionSummary",
            )
        if not self.content_text or not self.content_text.strip():
            raise ValueError("FeedPost.content_text must be non-empty")
        if self.image_url is not None and not self.image_url.strip():
            raise ValueError(
                "FeedPost.image_url must be non-empty when provided",
            )
        if self.video_url is not None and not self.video_url.strip():
            raise ValueError(
                "FeedPost.video_url must be non-empty when provided",
            )
        # Normalise so persisted rows never carry stray whitespace.
        object.__setattr__(self, "content_text", self.content_text.strip())
        if self.image_url is not None:
            object.__setattr__(self, "image_url", self.image_url.strip())
        if self.image_prompt is not None:
            cleaned_prompt = self.image_prompt.strip() or None
            object.__setattr__(self, "image_prompt", cleaned_prompt)
        if self.video_url is not None:
            object.__setattr__(self, "video_url", self.video_url.strip())
        if self.video_prompt is not None:
            cleaned_video = self.video_prompt.strip() or None
            object.__setattr__(self, "video_prompt", cleaned_video)

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        kind: FeedKind | str,
        content_text: str,
        source: FeedSource,
        image_url: str | None = None,
        image_prompt: str | None = None,
        video_url: str | None = None,
        video_prompt: str | None = None,
        created_at: datetime | None = None,
        reactions: FeedReactionSummary | None = None,
        reactions_seen_at: datetime | None = None,
        id: str | None = None,
    ) -> "FeedPost":
        resolved_kind = (
            kind if isinstance(kind, FeedKind) else FeedKind.from_string(kind)
        )
        return cls(
            id=id or uuid4().hex,
            character_id=character_id,
            kind=resolved_kind,
            content_text=content_text,
            source=source,
            created_at=created_at or _utcnow(),
            image_url=image_url,
            image_prompt=image_prompt,
            video_url=video_url,
            video_prompt=video_prompt,
            reactions=reactions or FeedReactionSummary(),
            reactions_seen_at=reactions_seen_at,
        )

    def with_image(
        self,
        *,
        image_url: str | None,
        image_prompt: str | None = None,
    ) -> "FeedPost":
        return replace(
            self,
            image_url=image_url,
            image_prompt=(
                image_prompt if image_prompt is not None else self.image_prompt
            ),
        )

    def with_video(
        self,
        *,
        video_url: str | None,
        video_prompt: str | None = None,
    ) -> "FeedPost":
        return replace(
            self,
            video_url=video_url,
            video_prompt=(
                video_prompt if video_prompt is not None else self.video_prompt
            ),
        )

    def with_reactions(self, reactions: FeedReactionSummary) -> "FeedPost":
        return replace(self, reactions=reactions)

    def mark_reactions_seen(self, *, when: datetime | None = None) -> "FeedPost":
        return replace(self, reactions_seen_at=when or _utcnow())

    def with_fields(
        self,
        *,
        content_text: str | None = None,
        image_url: Any = _UNSET,
        image_prompt: Any = _UNSET,
    ) -> "FeedPost":
        next_image_url = (
            self.image_url if image_url is _UNSET else image_url
        )
        next_image_prompt = (
            self.image_prompt if image_prompt is _UNSET else image_prompt
        )
        return replace(
            self,
            content_text=(
                content_text.strip() if content_text is not None else self.content_text
            ),
            image_url=next_image_url,
            image_prompt=next_image_prompt,
        )
