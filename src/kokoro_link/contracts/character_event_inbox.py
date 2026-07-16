"""Repository contract for per-character event inbox rows.

The ``claim`` method is the linchpin: it must atomically transition
``claimed_by_surface`` from ``None`` to a concrete value. Concurrent
calls from the proactive dispatcher and feed composer for the same row
must result in exactly one win; the loser receives ``None``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.character_event_inbox import (
    CharacterEventInboxItem,
)


class CharacterEventInboxRepositoryPort(Protocol):
    async def add_many(
        self, items: list[CharacterEventInboxItem],
    ) -> None: ...

    async def list_for_character(
        self,
        character_id: str,
        *,
        unclaimed_only: bool = False,
        surface: str | None = None,
        limit: int | None = None,
    ) -> list[CharacterEventInboxItem]:
        """List inbox items for a character.

        ``unclaimed_only=True`` filters to rows where
        ``claimed_by_surface IS NULL``. ``surface`` (when set) filters
        to rows already claimed by that surface (exclusive with the
        ``unclaimed_only`` flag — caller passes one or the other).
        Ordering: oldest first (FIFO) so dispensers that ``claim`` the
        first item naturally clear the queue."""

    async def claim(
        self, item_id: str, *, surface: str, at: datetime,
    ) -> CharacterEventInboxItem | None:
        """Atomically transition a row from unclaimed to claimed-by-``surface``.

        Returns the updated item on success, ``None`` if the row was
        already claimed (race lost) or no longer exists. Implementations
        must use ``UPDATE ... WHERE claimed_by_surface IS NULL`` so the
        decision is made in the database, not in Python."""

    async def release(
        self, item_id: str, *, surface: str,
    ) -> bool:
        """Atomically reverse a claim, if and only if the row is still
        owned by ``surface``. Returns ``True`` on success, ``False`` if
        the row no longer exists or was claimed by someone else (never
        steals from another surface). Implementations must use
        ``UPDATE ... WHERE claimed_by_surface = :surface`` so the check
        happens in the database."""

    async def count_unclaimed(self, character_id: str) -> int: ...

    async def trim_oldest(
        self, character_id: str, *, keep: int,
    ) -> int:
        """Keep at most ``keep`` rows for ``character_id`` (oldest first
        deleted). Returns count deleted. Used to bound inbox size after
        a curator pass writes new rows."""

    async def delete_older_than(self, cutoff: datetime) -> int: ...

    async def delete_for_event(self, world_event_id: str) -> int: ...

    async def has_event(
        self, character_id: str, world_event_id: str,
    ) -> bool:
        """True if the character already has an inbox row for this
        event (claimed or not). Curator uses this to skip re-adding a
        seed the character already saw."""
