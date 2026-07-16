"""DTOs for the character feed-wall HTTP surface.

The feed list endpoint returns reverse-chronological pages, each item
carrying the post body, optional image, source provenance, and a
denormalised reaction summary so the frontend doesn't need a second
roundtrip per card.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from pydantic import BaseModel

from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.feed_source import FeedSource


class FeedSourceResponse(BaseModel):
    kind: str
    ref_id: str | None = None

    @classmethod
    def from_domain(cls, source: FeedSource) -> "FeedSourceResponse":
        return cls(kind=source.kind, ref_id=source.ref_id)


class FeedReactionSummaryResponse(BaseModel):
    likes: int = 0
    comments: int = 0


class FeedPostResponse(BaseModel):
    id: str
    character_id: str
    kind: str
    content_text: str
    source: FeedSourceResponse
    image_url: str | None = None
    image_prompt: str | None = None
    video_url: str | None = None
    video_prompt: str | None = None
    reactions: FeedReactionSummaryResponse
    reactions_seen_at: datetime | None = None
    created_at: datetime
    liked: bool = False
    """Whether the calling user has liked this post. Hydrated by the
    feed routes from ``FeedReactionRepositoryPort.liked_post_ids`` so
    the frontend can render the heart state without a per-card
    follow-up request."""

    @classmethod
    def from_domain(
        cls, post: FeedPost, *, liked: bool = False,
    ) -> "FeedPostResponse":
        return cls(
            id=post.id,
            character_id=post.character_id,
            kind=post.kind.value,
            content_text=post.content_text,
            source=FeedSourceResponse.from_domain(post.source),
            image_url=post.image_url,
            image_prompt=post.image_prompt,
            video_url=post.video_url,
            video_prompt=post.video_prompt,
            reactions=FeedReactionSummaryResponse(
                likes=post.reactions.likes,
                comments=post.reactions.comments,
            ),
            reactions_seen_at=post.reactions_seen_at,
            created_at=post.created_at,
            liked=liked,
        )


class FeedListResponse(BaseModel):
    items: list[FeedPostResponse]
    has_more: bool
    next_before: datetime | None = None
    """The ``created_at`` of the oldest item in this page; pass back as
    ``before`` to fetch the next page. ``None`` when ``has_more`` is
    false so the client can short-circuit pagination."""

    @classmethod
    def from_domain(
        cls,
        posts: Sequence[FeedPost],
        *,
        limit: int,
        liked_post_ids: set[str] | None = None,
    ) -> "FeedListResponse":
        liked_set = liked_post_ids or set()
        items = [
            FeedPostResponse.from_domain(p, liked=(p.id in liked_set))
            for p in posts
        ]
        has_more = len(posts) >= limit
        next_before = posts[-1].created_at if has_more and posts else None
        return cls(
            items=items, has_more=has_more, next_before=next_before,
        )
