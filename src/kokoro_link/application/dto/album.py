"""DTOs for the character album HTTP surface."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from pydantic import BaseModel

from kokoro_link.domain.entities.album_item import AlbumItem


class AlbumItemResponse(BaseModel):
    id: str
    character_id: str
    url: str
    source: str
    caption: str | None = None
    byte_size: int | None = None
    created_at: datetime

    @classmethod
    def from_domain(cls, item: AlbumItem) -> "AlbumItemResponse":
        return cls(
            id=item.id,
            character_id=item.character_id,
            url=item.url,
            source=item.source,
            caption=item.caption,
            byte_size=item.byte_size,
            created_at=item.created_at,
        )


class AlbumListResponse(BaseModel):
    items: list[AlbumItemResponse]
    total: int
    """Total row count for the character, independent of page size —
    the UI shows this as the album's overall size, not this page's."""
    has_more: bool
    next_before: datetime | None = None
    """The ``created_at`` of the oldest item in this page; pass back as
    ``before`` to fetch the next page. ``None`` when ``has_more`` is
    false so the client can short-circuit pagination. Same keyset shape
    as ``FeedListResponse`` — see ``application/dto/feed.py``."""

    @classmethod
    def from_domain(
        cls,
        items: Sequence[AlbumItem],
        *,
        total: int,
        limit: int,
    ) -> "AlbumListResponse":
        has_more = len(items) >= limit
        next_before = items[-1].created_at if has_more and items else None
        return cls(
            items=[AlbumItemResponse.from_domain(i) for i in items],
            total=total,
            has_more=has_more,
            next_before=next_before,
        )


class TransferFromStageRequest(BaseModel):
    url: str
