from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.rss_ingestion_service import (
    RssIngestionService,
)
from kokoro_link.contracts.embedder import EmbedderError
from kokoro_link.domain.entities.character_event_mention import (
    CharacterEventMention,
)
from kokoro_link.infrastructure.repositories.in_memory_character_event_mentions import (  # noqa: E501
    InMemoryCharacterEventMentionRepository,
)
from kokoro_link.contracts.rss_feed_fetcher import RawWorldEvent, RssFetchError
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


class _PerSourceFetcher(_Fetcher):
    async def fetch(self, **kwargs) -> list[RawWorldEvent]:
        if kwargs["source_id"] == "broken":
            raise RssFetchError(
                "http_status", "upstream returned HTTP 410", status_code=410,
            )
        return await super().fetch(**kwargs)


@pytest.mark.asyncio
async def test_fetch_failure_is_source_isolated_and_report_is_url_safe() -> None:
    sources = InMemoryRssSourceRepository()
    events = InMemoryWorldEventRepository()
    broken_url = "https://example.test/feed?token=must-not-leak"
    await sources.upsert(RssSource(
        id="broken", name="Broken", feed_url=broken_url, category="news",
    ))
    await sources.upsert(RssSource(
        id="healthy", name="Healthy", feed_url="https://healthy.test/rss",
        category="news",
    ))
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=events,
        feed_fetcher=_PerSourceFetcher(),
        embedder=_Embedder(),
    )

    report = await service.ingest_all()

    assert report.sources_attempted == 2
    assert report.sources_succeeded == 1
    assert report.events_persisted == 1
    assert report.errors == (
        "broken: http_status: upstream returned HTTP 410",
    )
    assert broken_url not in repr(report.errors)
    assert "token" not in repr(report.errors)
    assert (await sources.get("broken")).last_error == (
        "http_status: upstream returned HTTP 410"
    )
    assert (await sources.get("healthy")).last_success_at is not None


class _UrlLeakingFetcher(_Fetcher):
    async def fetch(self, **kwargs) -> list[RawWorldEvent]:
        raise RuntimeError("failed https://example.test/rss?secret=do-not-leak")


@pytest.mark.asyncio
async def test_unexpected_fetch_error_does_not_persist_exception_message() -> None:
    sources = InMemoryRssSourceRepository()
    await sources.upsert(_NCDR)
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=InMemoryWorldEventRepository(),
        feed_fetcher=_UrlLeakingFetcher(),
        embedder=_Embedder(),
    )

    report = await service.ingest_all()

    assert report.errors == ("ncdr: unexpected_fetch_error: RuntimeError",)
    assert "example.test" not in (await sources.get("ncdr")).last_error


class _FailingEventRepository(InMemoryWorldEventRepository):
    async def upsert(self, event: WorldEvent) -> None:
        raise RuntimeError("write failed for https://private.test/event?key=secret")


@pytest.mark.asyncio
async def test_persist_failure_is_counted_without_logging_event_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sources = InMemoryRssSourceRepository()
    await sources.upsert(_NCDR)
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=_FailingEventRepository(),
        feed_fetcher=_Fetcher(),
        embedder=_Embedder(),
    )

    with caplog.at_level("WARNING"):
        report = await service.ingest_all()

    assert report.events_failed_persist == 1
    assert report.events_persisted == 0
    assert report.sources_succeeded == 0
    assert report.errors == (
        "ncdr: storage_degraded: persisted=0 failed=1",
    )
    source = await sources.get("ncdr")
    assert source.last_success_at is None
    assert source.last_error == "storage_degraded: persisted=0 failed=1"
    assert source.fetched_count_total == 0
    assert "world_event upsert failed source_id=ncdr" in caplog.text
    assert "private.test" not in caplog.text
    assert "example.com/alert" not in caplog.text


class _PartiallyFailingEventRepository(InMemoryWorldEventRepository):
    async def upsert(self, event: WorldEvent) -> None:
        if event.url.endswith("/failed"):
            raise RuntimeError("write failed")
        await super().upsert(event)


class _TwoEventFetcher(_Fetcher):
    async def fetch(self, **kwargs) -> list[RawWorldEvent]:
        event = (await super().fetch(**kwargs))[0]
        return [
            replace(event, url="https://events.test/persisted"),
            replace(event, url="https://events.test/failed"),
        ]


@pytest.mark.asyncio
async def test_partial_persist_failure_marks_degraded_and_counts_only_writes(
) -> None:
    sources = InMemoryRssSourceRepository()
    await sources.upsert(_NCDR)
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=_PartiallyFailingEventRepository(),
        feed_fetcher=_TwoEventFetcher(),
        embedder=_Embedder(),
    )

    report = await service.ingest_all()

    assert report.events_persisted == 1
    assert report.events_failed_persist == 1
    assert report.sources_succeeded == 0
    assert report.errors == (
        "ncdr: storage_degraded: persisted=1 failed=1",
    )
    source = await sources.get("ncdr")
    assert source.last_success_at is None
    assert source.last_error == "storage_degraded: persisted=1 failed=1"
    assert source.fetched_count_total == 1


class _FailingEmbedder(_Embedder):
    @property
    def is_operational(self) -> bool:
        return True

    async def embed_many(self, texts):
        raise EmbedderError("provider URL or credential must not be logged")


@pytest.mark.asyncio
async def test_embed_batch_failure_is_counted_while_events_persist() -> None:
    sources = InMemoryRssSourceRepository()
    events = InMemoryWorldEventRepository()
    await sources.upsert(_NCDR)
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=events,
        feed_fetcher=_Fetcher(),
        embedder=_FailingEmbedder(),
    )

    report = await service.ingest_all()

    assert report.embed_batches_failed == 1
    assert report.events_skipped_embed == 1
    assert report.events_persisted == 1


class _ShortEmbedder(_Embedder):
    @property
    def is_operational(self) -> bool:
        return True

    async def embed_many(self, texts):
        _ = texts
        return []


@pytest.mark.asyncio
async def test_short_embed_batch_cannot_silently_drop_events() -> None:
    sources = InMemoryRssSourceRepository()
    events = InMemoryWorldEventRepository()
    await sources.upsert(_NCDR)
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=events,
        feed_fetcher=_Fetcher(),
        embedder=_ShortEmbedder(),
    )

    report = await service.ingest_all()

    assert report.embed_batches_failed == 1
    assert report.events_skipped_embed == 1
    assert report.events_persisted == 1


class _PerSourceProbeFailureRepository(InMemoryWorldEventRepository):
    async def has_url(self, url: str) -> bool:
        if "broken-event" in url:
            raise RuntimeError(
                "probe failed https://private.test/?credential=must-not-leak"
            )
        return await super().has_url(url)


class _DistinctPerSourceFetcher(_Fetcher):
    async def fetch(self, **kwargs) -> list[RawWorldEvent]:
        event = (await super().fetch(**kwargs))[0]
        return [replace(
            event,
            url=f"https://events.test/{kwargs['source_id']}-event",
        )]


@pytest.mark.asyncio
async def test_post_fetch_failure_is_source_isolated_and_url_safe() -> None:
    sources = InMemoryRssSourceRepository()
    events = _PerSourceProbeFailureRepository()
    for source_id in ("broken", "healthy"):
        await sources.upsert(RssSource(
            id=source_id,
            name=source_id.title(),
            feed_url=f"https://feeds.test/{source_id}?token=do-not-leak",
            category="news",
        ))
    service = RssIngestionService(
        rss_source_repository=sources,
        world_event_repository=events,
        feed_fetcher=_DistinctPerSourceFetcher(),
        embedder=_Embedder(),
    )

    report = await service.ingest_all()

    assert report.sources_attempted == 2
    assert report.sources_succeeded == 1
    assert report.events_persisted == 1
    assert report.errors == (
        "broken: source_processing_error: RuntimeError",
    )
    assert "private.test" not in repr(report.errors)
    assert "feeds.test" not in repr(report.errors)
    assert (await sources.get("broken")).last_error == (
        "source_processing_error: RuntimeError"
    )
    assert (await sources.get("healthy")).last_success_at is not None


@pytest.mark.asyncio
async def test_gc_also_prunes_stale_event_mentions() -> None:
    """Mentions point into the pool this GC empties.

    PostgreSQL cascades them away with the event, but self-host SQLite
    never enforces the foreign key, so without this sweep the mention
    table is the one thing that only ever grows there.
    """
    mentions = InMemoryCharacterEventMentionRepository()
    now = datetime.now(timezone.utc)
    for event_id, age_days in (("old", 90), ("fresh", 1)):
        await mentions.record(
            CharacterEventMention.create(
                character_id="char-1",
                world_event_id=event_id,
                surface="proactive_message",
                mentioned_at=now - timedelta(days=age_days),
            ),
        )
    service = RssIngestionService(
        rss_source_repository=InMemoryRssSourceRepository(),
        world_event_repository=InMemoryWorldEventRepository(),
        feed_fetcher=_Fetcher(),  # type: ignore[arg-type]
        embedder=_Embedder(),  # type: ignore[arg-type]
        event_mention_repository=mentions,
    )

    await service.gc()

    remaining = await mentions.list_recent("char-1")
    assert [row.world_event_id for row in remaining] == ["fresh"]


@pytest.mark.asyncio
async def test_gc_survives_a_broken_mention_store() -> None:
    class _Exploding(InMemoryCharacterEventMentionRepository):
        async def delete_older_than(self, cutoff) -> int:  # noqa: ANN001
            raise RuntimeError("mention store down")

    events = InMemoryWorldEventRepository()
    service = RssIngestionService(
        rss_source_repository=InMemoryRssSourceRepository(),
        world_event_repository=events,
        feed_fetcher=_Fetcher(),  # type: ignore[arg-type]
        embedder=_Embedder(),  # type: ignore[arg-type]
        event_mention_repository=_Exploding(),
    )

    assert await service.gc() == 0
