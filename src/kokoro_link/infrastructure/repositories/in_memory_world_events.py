"""In-memory world event repository for tests and no-database mode."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.world_event import WorldEvent


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

    async def list_with_embeddings_in_window(
        self,
        *,
        since: datetime,
        categories: list[str] | None = None,
        limit: int = 500,
    ) -> list[WorldEvent]:
        cats = (
            {c.strip().lower() for c in categories if c.strip()}
            if categories else None
        )
        events = [
            e for e in self._store.values()
            if e.embedding is not None
            and e.published_at >= since
            and (cats is None or (e.category or "news").lower() in cats)
        ]
        events.sort(key=lambda e: e.published_at, reverse=True)
        return events[:limit]
