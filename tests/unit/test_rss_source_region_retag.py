"""Admin region edits reach the events already in the pool.

``world_events`` denormalises ``rss_sources.region`` at ingest time and has
no foreign key back to the registry — the only back-link is the source
*name* copied onto each row. Re-tagging on that name is therefore
best-effort by construction, and the service refuses the update whenever
the name is ambiguous (two registry entries share it) rather than
mis-tagging another feed's events. Whatever it cannot reach is repaired by
the ingest service's dedup-hit self-heal on the next pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.rss_source_region_retag_service import (
    RssSourceRegionRetagService,
)
from kokoro_link.domain.entities.rss_source import RssSource
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.infrastructure.repositories.in_memory_rss_sources import (
    InMemoryRssSourceRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_world_events import (
    InMemoryWorldEventRepository,
)


def _event(event_id: str, *, source: str, region: str | None) -> WorldEvent:
    ago = datetime.now(timezone.utc) - timedelta(hours=1)
    return WorldEvent(
        id=event_id,
        source=source,
        title=f"title-{event_id}",
        summary="",
        url=f"https://example.com/{event_id}",
        published_at=ago,
        fetched_at=ago,
        category="news",
        source_region=region,
    )


async def _build(
    *sources: RssSource,
) -> tuple[
    RssSourceRegionRetagService,
    InMemoryRssSourceRepository,
    InMemoryWorldEventRepository,
]:
    registry = InMemoryRssSourceRepository()
    for source in sources:
        await registry.upsert(source)
    events = InMemoryWorldEventRepository()
    service = RssSourceRegionRetagService(
        rss_source_repository=registry,
        world_event_repository=events,
    )
    return service, registry, events


_CNA = RssSource(
    id="cna-life",
    name="中央社生活",
    feed_url="https://example.com/cna",
    category="news",
)


@pytest.mark.asyncio
async def test_retags_events_ingested_under_that_source_name() -> None:
    service, _, events = await _build(_CNA)
    await events.upsert(_event("a", source="中央社生活", region=None))
    await events.upsert(_event("b", source="中央社生活", region=None))

    retagged = await service.retag(ingested_source_name="中央社生活", region="tw")

    assert retagged == 2
    assert (await events.get("a")).source_region == "TW"
    assert (await events.get("b")).source_region == "TW"


@pytest.mark.asyncio
async def test_retag_clears_region_back_to_global() -> None:
    service, _, events = await _build(_CNA)
    await events.upsert(_event("a", source="中央社生活", region="TW"))

    retagged = await service.retag(ingested_source_name="中央社生活", region=None)

    assert retagged == 1
    assert (await events.get("a")).source_region is None


@pytest.mark.asyncio
async def test_retag_leaves_other_sources_events_alone() -> None:
    service, _, events = await _build(_CNA)
    await events.upsert(_event("mine", source="中央社生活", region=None))
    await events.upsert(_event("theirs", source="NPR", region=None))

    await service.retag(ingested_source_name="中央社生活", region="TW")

    assert (await events.get("theirs")).source_region is None


@pytest.mark.asyncio
async def test_retag_refuses_when_the_name_is_ambiguous() -> None:
    """Two registry entries sharing a display name make the name join
    unsound — retagging would move another feed's events into the wrong
    region. Better to leave them for the ingest self-heal."""
    twin = RssSource(
        id="cna-mirror",
        name="中央社生活",
        feed_url="https://example.com/mirror",
        category="news",
    )
    service, _, events = await _build(_CNA, twin)
    await events.upsert(_event("a", source="中央社生活", region=None))

    retagged = await service.retag(ingested_source_name="中央社生活", region="TW")

    assert retagged == 0
    assert (await events.get("a")).source_region is None


@pytest.mark.asyncio
async def test_retag_is_a_noop_when_already_on_the_target_region() -> None:
    service, _, events = await _build(_CNA)
    await events.upsert(_event("a", source="中央社生活", region="TW"))

    assert await service.retag(ingested_source_name="中央社生活", region="tw") == 0
