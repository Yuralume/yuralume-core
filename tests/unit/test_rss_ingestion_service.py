from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.rss_ingestion_service import (
    RssIngestionService,
)
from kokoro_link.contracts.rss_feed_fetcher import RawWorldEvent
from kokoro_link.domain.entities.rss_source import RssSource
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.infrastructure.repositories.in_memory_rss_sources import (
    InMemoryRssSourceRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_world_events import (
    InMemoryWorldEventRepository,
)


class _Fetcher:
    def __init__(self, *, published_at: datetime | None = None) -> None:
        self.locale_seen: str | None = None
        self._published_at = published_at

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
            published_at=(
                self._published_at
                if self._published_at is not None
                else datetime.now(timezone.utc) - timedelta(hours=1)
            ),
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


async def _build(
    source: RssSource, fetcher: _Fetcher,
) -> tuple[RssIngestionService, InMemoryWorldEventRepository]:
    sources = InMemoryRssSourceRepository()
    events = InMemoryWorldEventRepository()
    await sources.upsert(source)
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=events,
        feed_fetcher=fetcher,  # type: ignore[arg-type]
        embedder=_Embedder(),  # type: ignore[arg-type]
    )
    return service, events


_NCDR = RssSource(
    id="ncdr",
    name="NCDR",
    feed_url="https://example.com/rss",
    category="emergency",
    locale="zh-TW",
    enabled=True,
)


@pytest.mark.asyncio
async def test_ingestion_persists_source_locale_on_world_event() -> None:
    fetcher = _Fetcher()
    service, events = await _build(_NCDR, fetcher)

    report = await service.ingest_all()
    stored = await events.query_recent(limit=1)

    assert report.events_persisted == 1
    assert fetcher.locale_seen == "zh-TW"
    assert stored[0].locale == "zh-TW"


@pytest.mark.asyncio
async def test_ingestion_denormalises_source_region_onto_world_event() -> None:
    """Region lives on the registry entry; the curator reads it off the
    event, so ingest is where it gets copied down."""
    service, events = await _build(replace(_NCDR, region="tw"), _Fetcher())

    await service.ingest_all()
    stored = await events.query_recent(limit=1)

    assert stored[0].source_region == "TW"


@pytest.mark.asyncio
async def test_ingestion_leaves_source_region_null_for_global_source() -> None:
    service, events = await _build(_NCDR, _Fetcher())

    await service.ingest_all()
    stored = await events.query_recent(limit=1)

    assert stored[0].source_region is None


@pytest.mark.asyncio
async def test_epoch_zero_publish_date_falls_back_to_fetch_time() -> None:
    """Whole feeds ship ``pubDate`` as the Unix epoch. Trusting it drops
    every item on the freshness cutoff — the feed silently vanishes."""
    before = datetime.now(timezone.utc)
    service, events = await _build(
        _NCDR, _Fetcher(published_at=datetime(1970, 1, 1, tzinfo=timezone.utc)),
    )

    report = await service.ingest_all()
    stored = await events.query_recent(limit=1)

    assert report.events_persisted == 1
    assert stored[0].published_at >= before


@pytest.mark.asyncio
async def test_far_future_publish_date_falls_back_to_fetch_time() -> None:
    """The mirror failure: a bogus future date would pin the item to the
    top of every recency-ordered read forever."""
    now = datetime.now(timezone.utc)
    service, events = await _build(
        _NCDR, _Fetcher(published_at=now + timedelta(days=400)),
    )

    await service.ingest_all()
    stored = await events.query_recent(limit=1)

    assert stored[0].published_at <= now + timedelta(days=1)


@pytest.mark.asyncio
async def test_plausible_publish_date_is_preserved() -> None:
    published = datetime.now(timezone.utc) - timedelta(hours=6)
    service, events = await _build(_NCDR, _Fetcher(published_at=published))

    await service.ingest_all()
    stored = await events.query_recent(limit=1)

    assert stored[0].published_at == published


# --- dedup-hit region self-heal --------------------------------------------
#
# ``world_events`` carries no back-link to ``rss_sources`` (only the source
# *name*), so an event already in the pool keeps whatever region it was
# ingested with. Re-seeing the URL on a later pass is the one moment the
# ingest service can repair it without a join key.


_FEED_URL = "https://example.com/alert"


async def _seed_existing(
    events: InMemoryWorldEventRepository, *, source_region: str | None,
) -> None:
    ago = datetime.now(timezone.utc) - timedelta(hours=2)
    await events.upsert(WorldEvent(
        id="already-ingested",
        source="NCDR",
        title="NCDR 颱風示警",
        summary="",
        url=_FEED_URL,
        published_at=ago,
        fetched_at=ago,
        category="emergency",
        source_region=source_region,
    ))


@pytest.mark.asyncio
async def test_dedup_hit_repairs_stale_source_region() -> None:
    """Events ingested before the source was region-tagged stay global and
    leak across regions until GC; the next pass over the same URL fixes it."""
    service, events = await _build(replace(_NCDR, region="tw"), _Fetcher())
    await _seed_existing(events, source_region=None)

    report = await service.ingest_all()

    assert report.events_skipped_dedup == 1
    assert report.events_persisted == 0
    assert report.events_region_corrected == 1
    assert (await events.get("already-ingested")).source_region == "TW"


@pytest.mark.asyncio
async def test_dedup_hit_clears_region_when_source_became_global() -> None:
    """The mirror failure: a mis-tagged source hides its events from every
    player until the operator's clear reaches the already-ingested rows."""
    service, events = await _build(_NCDR, _Fetcher())
    await _seed_existing(events, source_region="TW")

    report = await service.ingest_all()

    assert report.events_region_corrected == 1
    assert (await events.get("already-ingested")).source_region is None


@pytest.mark.asyncio
async def test_dedup_hit_with_matching_region_corrects_nothing() -> None:
    service, events = await _build(replace(_NCDR, region="TW"), _Fetcher())
    await _seed_existing(events, source_region="TW")

    report = await service.ingest_all()

    assert report.events_skipped_dedup == 1
    assert report.events_region_corrected == 0
