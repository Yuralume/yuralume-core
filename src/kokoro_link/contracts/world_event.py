"""Repository contract for world events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.world_event import WorldEvent


class WorldEventRepositoryPort(Protocol):
    async def query_recent(
        self,
        *,
        limit: int,
        topic_tags: list[str] | None = None,
        max_age_days: int | None = None,
    ) -> list[WorldEvent]: ...

    async def upsert(self, event: WorldEvent) -> None: ...

    async def delete_older_than(self, cutoff: datetime) -> int: ...

    async def get(self, event_id: str) -> WorldEvent | None: ...

    async def has_url(self, url: str) -> bool:
        """De-dup probe used by the ingest service to skip events whose
        URL was already persisted (RSS feeds republish entries on
        edits / re-orderings)."""

    async def sync_source_region(
        self, *, urls: Sequence[str], source_region: str | None,
    ) -> int:
        """Re-point already-persisted events at their source's *current*
        region. Returns the number of rows actually changed.

        ``source_region`` is denormalised at ingest time and the pool has
        no back-link to ``rss_sources``, so an event the de-dup probe
        skipped keeps whatever region it was first written with — stale
        after a migration backfill or an admin re-tag, i.e. leaking into
        the wrong region's pool (or hidden from every pool) until GC. The
        ingest service passes the URLs it just skipped so the correction
        rides the pass that already proved those events are still live.
        Rows already on the target region must not be reported."""

    async def retag_source_region(
        self, *, source_name: str, source_region: str | None,
    ) -> int:
        """Re-tag every event ingested under ``source_name``. Returns the
        number of rows changed.

        The name is the only back-link the pool carries, so callers are
        responsible for establishing that it unambiguously identifies one
        registry entry (see ``RssSourceRegionRetagService``)."""

    async def list_with_embeddings_in_window(
        self,
        *,
        since: datetime,
        categories: list[str] | None = None,
        limit: int = 500,
        operator_region: str | None = None,
    ) -> list[WorldEvent]:
        """List events with non-null embeddings published since
        ``since``. ``categories`` (when non-empty) filters to those
        categories only. The curator pulls a window then ranks in
        Python — pgvector can rank server-side, but the per-character
        compute is small (≤ 500 × 1024 floats) and Python ranking keeps
        the port DB-agnostic.

        ``operator_region`` (when set) narrows the pool to events whose
        ``source_region`` is ``NULL`` (global) or equals that code,
        case-insensitively. ``None`` — the self-host default, where the
        operator has no ``country_code`` — applies no region filter at
        all. The narrowing must happen *before* ``limit`` so a
        high-volume neighbouring region can't starve the window."""
