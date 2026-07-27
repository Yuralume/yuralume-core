"""Thread-safe in-memory ``StoryArcRepositoryPort`` implementation.

Mirrors the shape of ``InMemoryStoryEventRepository`` / ``InMemoryStorySeedRepository``
so tests can drop in either backend without conditional wiring.

It is also the unit twin of the SA adapter's single-active-arc invariant: writes
that would leave a character with two ``active`` arcs raise ``ActiveArcConflict``
exactly as ``uq_story_arcs_active_character`` makes them do on PostgreSQL /
SQLite, so a caller that mishandles the race fails in unit tests rather than
only in production.
"""

from __future__ import annotations

import threading
from copy import copy

from kokoro_link.contracts.story_arc import (
    ActiveArcConflict,
    StoryArcRepositoryPort,
)
from kokoro_link.domain.entities.story_arc import ARC_ACTIVE, StoryArc


class InMemoryStoryArcRepository(StoryArcRepositoryPort):
    def __init__(self) -> None:
        self._arcs: dict[str, StoryArc] = {}
        self._lock = threading.RLock()

    async def add(self, arc: StoryArc) -> None:
        with self._lock:
            if arc.id in self._arcs:
                raise ValueError(f"StoryArc id {arc.id!r} already exists")
            self._reject_second_active(arc)
            self._arcs[arc.id] = arc

    def _reject_second_active(self, arc: StoryArc) -> None:
        """Twin of ``uq_story_arcs_active_character``. Caller holds the lock."""
        if arc.status != ARC_ACTIVE:
            return
        for other in self._arcs.values():
            if (
                other.id != arc.id
                and other.character_id == arc.character_id
                and other.status == ARC_ACTIVE
            ):
                raise ActiveArcConflict(arc.character_id)

    async def get(self, arc_id: str) -> StoryArc | None:
        with self._lock:
            return self._arcs.get(arc_id)

    async def get_active_for_character(
        self, character_id: str,
    ) -> StoryArc | None:
        with self._lock:
            actives = [
                a for a in self._arcs.values()
                if a.character_id == character_id and a.status == ARC_ACTIVE
            ]
        if not actives:
            return None
        # ``_reject_second_active`` keeps this list at length 1; the sort is a
        # belt-and-braces match for the SA adapter's ``updated_at DESC LIMIT 1``.
        actives.sort(key=lambda a: a.updated_at, reverse=True)
        return actives[0]

    async def list_for_character(
        self, character_id: str,
    ) -> list[StoryArc]:
        with self._lock:
            matches = [
                a for a in self._arcs.values() if a.character_id == character_id
            ]
        matches.sort(key=lambda a: a.updated_at, reverse=True)
        return matches

    async def save(self, arc: StoryArc) -> None:
        with self._lock:
            self._reject_second_active(arc)
            self._arcs[arc.id] = copy(arc)

    async def delete(self, arc_id: str) -> None:
        with self._lock:
            self._arcs.pop(arc_id, None)

    async def delete_for_character(self, character_id: str) -> int:
        with self._lock:
            victims = [
                aid for aid, a in self._arcs.items() if a.character_id == character_id
            ]
            for aid in victims:
                del self._arcs[aid]
        return len(victims)

    async def find_by_beat_id(self, beat_id: str) -> StoryArc | None:
        with self._lock:
            for arc in self._arcs.values():
                if any(b.id == beat_id for b in arc.beats):
                    return arc
        return None
