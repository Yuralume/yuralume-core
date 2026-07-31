"""PostgreSQL concurrency regressions for the world-event repository."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.infrastructure.persistence.sa_world_event_repository import (
    SaWorldEventRepository,
)


def _event(event_id: str, url: str, *, title: str) -> WorldEvent:
    now = datetime(2026, 7, 28, 5, 0, tzinfo=timezone.utc)
    return WorldEvent(
        id=event_id,
        source="test-feed",
        title=title,
        summary=f"summary for {title}",
        url=url,
        published_at=now,
        fetched_at=now,
        topic_tags=("test",),
    )


@pytest.mark.asyncio
async def test_same_url_is_atomic_across_repository_workers(
    session_factory: sessionmaker,
) -> None:
    """Two workers use distinct sessions and may both miss ``has_url``."""
    worker_a = SaWorldEventRepository(session_factory)
    worker_b = SaWorldEventRepository(session_factory)
    url = "https://example.test/shared-story"

    assert not await worker_a.has_url(url)
    assert not await worker_b.has_url(url)

    await asyncio.gather(
        worker_a.upsert(_event("event-a", url, title="worker A")),
        worker_b.upsert(_event("event-b", url, title="worker B")),
    )

    stored = await worker_a.query_recent(limit=10)
    assert len(stored) == 1
    assert stored[0].url == url
    assert stored[0].id in {"event-a", "event-b"}
    assert stored[0].title in {"worker A", "worker B"}


@pytest.mark.asyncio
async def test_same_url_reingest_updates_without_replacing_stable_id(
    session_factory: sessionmaker,
) -> None:
    repo = SaWorldEventRepository(session_factory)
    original = _event(
        "stable-event-id",
        "https://example.test/updated-story",
        title="original title",
    )
    await repo.upsert(original)

    await repo.upsert(replace(
        original,
        id="racing-event-id",
        title="corrected title",
        summary="corrected summary",
    ))

    stored = await repo.query_recent(limit=10)
    assert len(stored) == 1
    assert stored[0].id == "stable-event-id"
    assert stored[0].title == "corrected title"
    assert stored[0].summary == "corrected summary"


@pytest.mark.asyncio
async def test_concurrent_distinct_urls_remain_distinct(
    session_factory: sessionmaker,
) -> None:
    worker_a = SaWorldEventRepository(session_factory)
    worker_b = SaWorldEventRepository(session_factory)

    await asyncio.gather(
        worker_a.upsert(_event(
            "event-one", "https://example.test/story-one", title="one",
        )),
        worker_b.upsert(_event(
            "event-two", "https://example.test/story-two", title="two",
        )),
    )

    stored = await worker_a.query_recent(limit=10)
    assert {event.id for event in stored} == {"event-one", "event-two"}
    assert {event.url for event in stored} == {
        "https://example.test/story-one",
        "https://example.test/story-two",
    }
