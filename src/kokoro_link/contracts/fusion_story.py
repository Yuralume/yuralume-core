"""Ports for the fusion-story (multi-character short story) layer.

Kept separate from ``contracts/story_arc.py`` and ``contracts/story.py``
because:

- arcs are calendar-bound, single-character narrative spines;
- story events / seeds are atomic daily gacha;
- fusion stories are stand-alone short-story compositions across many
  characters, with their own version-chain semantics.

All methods are async — the SA-backed implementation runs over the
shared async session factory, in-memory implementation just shadows the
shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kokoro_link.domain.entities.fusion_story import FusionStory


class FusionStoryRepositoryPort(ABC):
    """CRUD for ``FusionStory`` + its embedded beats + version chain.

    ``save`` is the workhorse: replaces the head row + rebuilds the beat
    + version-chain rows atomically (split into two transactions when
    backed by SA, mirroring ``SAStoryArcRepository``). Per-version /
    per-beat APIs would multiply the surface area without enabling any
    new use case the orchestrator needs.
    """

    @abstractmethod
    async def add(self, story: FusionStory) -> None: ...

    @abstractmethod
    async def get(self, story_id: str) -> FusionStory | None: ...

    @abstractmethod
    async def list_recent(self, *, limit: int = 50) -> list[FusionStory]:
        """Newest-first list for the index UI.

        Heavy on payload — each row carries its full text. The UI is
        expected to render summaries, not the body, but the simplest
        path for now is to load and let the frontend decide. Revisit
        with a dedicated ``list_summary`` if this becomes a bottleneck.
        """

    @abstractmethod
    async def save(self, story: FusionStory) -> None:
        """Upsert head row + rebuild beats + append new version snapshots."""

    @abstractmethod
    async def delete(self, story_id: str) -> None: ...
