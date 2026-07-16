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

    @classmethod
    def from_domain(cls, items: Sequence[AlbumItem]) -> "AlbumListResponse":
        return cls(
            items=[AlbumItemResponse.from_domain(i) for i in items],
            total=len(items),
        )


class TransferFromStageRequest(BaseModel):
    url: str
