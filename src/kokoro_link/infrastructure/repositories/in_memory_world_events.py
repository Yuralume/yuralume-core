"""In-memory world event repository for tests and no-database mode."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.domain.value_objects.region_code import normalise_region


class InMemoryWorldEventRepository:
    def __init__(self) -> None:
        self._store: dict[str, WorldEvent] = {}

    async def query_recent(
        self,
        *,
        limit: int,
        topic_tags: list[str] | None = None,
        max_age_days: int | None = None,
    ) -> list[WorldEvent]:
        now = datetime.now(timezone.utc)
        events = list(self._store.values())

        if max_age_days is not None:
            cutoff = now - timedelta(days=max_age_days)
            events = [e for e in events if e.published_at >= cutoff]

        if topic_tags:
            tag_set = {t.lower() for t in topic_tags}
            events = [
                e for e in events
                if any(t.lower() in tag_set for t in e.topic_tags)
            ] or events

        events.sort(key=lambda e: e.published_at, reverse=True)
        return events[:limit]

    async def upsert(self, event: WorldEvent) -> None:
        self._store[event.id] = event

    async def delete_older_than(self, cutoff: datetime) -> int:
        to_delete = [
            eid for eid, e in self._store.items() if e.fetched_at < cutoff
        ]
        for eid in to_delete:
            del self._store[eid]
        return len(to_delete)

    async def get(self, event_id: str) -> WorldEvent | None:
        return self._store.get(event_id)

    async def has_url(self, url: str) -> bool:
        return any(e.url == url for e in self._store.values())

    async def sync_source_region(
        self, *, urls: Sequence[str], source_region: str | None,
    ) -> int:
        wanted = set(urls)
        if not wanted:
            return 0
        return self._apply_region(
            lambda e: e.url in wanted, source_region,
        )

    async def retag_source_region(
        self, *, source_name: str, source_region: str | None,
    ) -> int:
        return self._apply_region(
            lambda e: e.source == source_name, source_region,
        )

    def _apply_region(
        self,
        matches: Callable[[WorldEvent], bool],
        source_region: str | None,
    ) -> int:
        region = normalise_region(source_region)
        changed = 0
        for event_id, event in list(self._store.items()):
            if not matches(event) or event.source_region == region:
                continue
            self._store[event_id] = replace(event, source_region=region)
            changed += 1
        return changed

    async def list_with_embeddings_in_window(
        self,
        *,
        since: datetime,
        categories: list[str] | None = None,
        limit: int = 500,
        operator_region: str | None = None,
    ) -> list[WorldEvent]:
        cats = (
            {c.strip().lower() for c in categories if c.strip()}
            if categories else None
        )
        region = normalise_region(operator_region)
        events = [
            e for e in self._store.values()
            if e.embedding is not None
            and e.published_at >= since
            and (cats is None or (e.category or "news").lower() in cats)
            and (
                region is None
                or e.source_region is None
                or e.source_region == region
            )
        ]
        events.sort(key=lambda e: e.published_at, reverse=True)
        return events[:limit]
