from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.rss_ingestion_service import (
    RssIngestionService,
)
from kokoro_link.contracts.rss_feed_fetcher import RawWorldEvent
from kokoro_link.domain.entities.rss_source import RssSource
from kokoro_link.infrastructure.repositories.in_memory_rss_sources import (
    InMemoryRssSourceRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_world_events import (
    InMemoryWorldEventRepository,
)


class _Fetcher:
    def __init__(self) -> None:
        self.locale_seen: str | None = None

    async def fetch(
        self,
        *,
        source_id: str,
        source_name: str,
        feed_url: str,
        category: str,
        locale: str | None = None,
    ) -> list[RawWorldEvent]:
        _ = feed_url
        self.locale_seen = locale
        return [RawWorldEvent(
            source_id=source_id,
            source_name=source_name,
            title="NCDR 颱風示警",
            summary="台灣發布強風豪雨警戒。",
            url="https://example.com/alert",
            published_at=datetime.now(timezone.utc) - timedelta(hours=1),
            category=category,
            locale=locale,
        )]


class _Embedder:
    @property
    def dimension(self) -> int:
        return 3

    @property
    def is_operational(self) -> bool:
        return False

    async def embed(self, text: str):  # pragma: no cover - unused
        _ = text
        return None

    async def embed_many(self, texts):  # pragma: no cover - unused
        _ = texts
        return []


@pytest.mark.asyncio
async def test_ingestion_persists_source_locale_on_world_event() -> None:
    sources = InMemoryRssSourceRepository()
    events = InMemoryWorldEventRepository()
    fetcher = _Fetcher()
    await sources.upsert(RssSource(
        id="ncdr",
        name="NCDR",
        feed_url="https://example.com/rss",
        category="emergency",
        locale="zh-TW",
        enabled=True,
    ))
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=events,
        feed_fetcher=fetcher,  # type: ignore[arg-type]
        embedder=_Embedder(),  # type: ignore[arg-type]
    )

    report = await service.ingest_all()
    stored = await events.query_recent(limit=1)

    assert report.events_persisted == 1
    assert fetcher.locale_seen == "zh-TW"
    assert stored[0].locale == "zh-TW"
