"""Thread-safe in-memory ``StoryArcRepositoryPort`` implementation.

Mirrors the shape of ``InMemoryStoryEventRepository`` / ``InMemoryStorySeedRepository``
so tests can drop in either backend without conditional wiring.
"""

from __future__ import annotations

import threading
from copy import copy

from kokoro_link.contracts.story_arc import StoryArcRepositoryPort
from kokoro_link.domain.entities.story_arc import ARC_ACTIVE, StoryArc


class InMemoryStoryArcRepository(StoryArcRepositoryPort):
    def __init__(self) -> None:
        self._arcs: dict[str, StoryArc] = {}
        self._lock = threading.RLock()

    async def add(self, arc: StoryArc) -> None:
        with self._lock:
            if arc.id in self._arcs:
                raise ValueError(f"StoryArc id {arc.id!r} already exists")
            self._arcs[arc.id] = arc

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
        # Multiple actives shouldn't happen in practice (service layer
        # keeps one at a time) — if it does, prefer the most recently
        # updated so the caller sees the latest planning output.
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
