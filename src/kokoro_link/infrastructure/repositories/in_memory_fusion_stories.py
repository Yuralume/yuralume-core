"""Thread-safe in-memory ``FusionStoryRepositoryPort`` implementation.

Mirrors the shape of ``InMemoryStoryArcRepository`` so unit tests and
the fake-provider dev flow exercise the same orchestration path as the
SA-backed repo.
"""

from __future__ import annotations

import threading
from copy import copy

from kokoro_link.contracts.fusion_story import FusionStoryRepositoryPort
from kokoro_link.domain.entities.fusion_story import FusionStory


class InMemoryFusionStoryRepository(FusionStoryRepositoryPort):
    def __init__(self) -> None:
        self._stories: dict[str, FusionStory] = {}
        self._lock = threading.RLock()

    async def add(self, story: FusionStory) -> None:
        with self._lock:
            if story.id in self._stories:
                raise ValueError(
                    f"FusionStory id {story.id!r} already exists",
                )
            self._stories[story.id] = copy(story)

    async def get(self, story_id: str) -> FusionStory | None:
        with self._lock:
            return self._stories.get(story_id)

    async def list_recent(self, *, limit: int = 50) -> list[FusionStory]:
        with self._lock:
            entries = list(self._stories.values())
        entries.sort(key=lambda s: s.updated_at, reverse=True)
        return entries[: max(1, limit)]

    async def save(self, story: FusionStory) -> None:
        with self._lock:
            self._stories[story.id] = copy(story)

    async def delete(self, story_id: str) -> None:
        with self._lock:
            self._stories.pop(story_id, None)
