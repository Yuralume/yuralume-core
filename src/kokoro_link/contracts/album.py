"""Port for the character album repository.

The album is append-mostly — tool generations push rows in, the
operator UI occasionally deletes or moves entries to/from the stage.
No semantic search, no ranking; ordering is always ``created_at DESC``
so the newest image lands at the top of the grid.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.album_item import AlbumItem


class AlbumRepositoryPort(Protocol):
    async def add(self, item: AlbumItem) -> None:
        """Insert a new album entry. ``item.id`` must be pre-assigned."""

    async def get(self, item_id: str) -> AlbumItem | None:
        """Fetch by id; returns ``None`` when absent."""

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AlbumItem]:
        """Newest-first listing, optionally paged.

        ``limit=None`` returns everything — fine for the album UI at
        current scale; paginate once a single character holds hundreds.
        """

    async def count_for_character(self, character_id: str) -> int:
        """Used by the service to enforce per-character sanity caps."""

    async def delete(self, item_id: str) -> bool:
        """Delete a single row. Returns True when a row was removed.

        Callers are responsible for removing the underlying file —
        the repository only manages the index.
        """

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade-delete all rows for a character. Returns the count
        removed; file deletion is the caller's job (same rule as
        ``delete``)."""
