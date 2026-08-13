"""Contract tests for the pending-video store's in-flight probe.

The probe runs on every feed tick of a deployment that can queue renders,
and it is the only thing standing between "one queued clip" and "a second
post composed five minutes later". Both backends run the same assertions:

- ``memory`` — the DB-less dev / unit twin.
- ``sqlite`` — the real SQLAlchemy adapter against ``sqlite+aiosqlite``.

The SQLite leg is what catches a statement that only compiles on the
dialect it was written against; the twin is what catches the two drifting.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.contracts.feed_video_jobs import (
    PENDING_VIDEO_STATUS_DEGRADED,
    PENDING_VIDEO_STATUS_PUBLISHED,
    PendingFeedVideo,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import PendingFeedVideoRow
from kokoro_link.infrastructure.persistence.sa_pending_feed_video_repository import (
    SAPendingFeedVideoRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_feed_videos import (
    InMemoryPendingFeedVideoRepository,
)

_NOW = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def repository(request):  # noqa: ANN001, ANN201
    if request.param == "memory":
        yield InMemoryPendingFeedVideoRepository()
        return
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PendingFeedVideoRow.__table__.create)
    try:
        yield SAPendingFeedVideoRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _record(*, character_id: str, record_id: str) -> PendingFeedVideo:
    return PendingFeedVideo.create(
        id=record_id,
        character_id=character_id,
        operator_id="op-1",
        job_id=f"job-{record_id}",
        content_text="想拍下這段路。",
        post_kind="mood",
        source_kind="silence",
        source_ref_id=None,
        first_frame_url=f"/v1/public/feed/{character_id}/frame.png",
        first_frame_key=f"feed/{character_id}/frame.png",
        image_prompt="neon street",
        video_prompt="slow dolly-in",
        submitted_at=_NOW,
    )


@pytest.mark.asyncio
async def test_no_rows_means_nothing_in_flight(repository) -> None:  # noqa: ANN001
    assert await repository.has_pending_for_character("aiko") is False


@pytest.mark.asyncio
async def test_a_pending_row_is_in_flight(repository) -> None:  # noqa: ANN001
    await repository.add(_record(character_id="aiko", record_id="p1"))

    assert await repository.has_pending_for_character("aiko") is True


@pytest.mark.asyncio
async def test_the_probe_is_scoped_to_one_character(repository) -> None:  # noqa: ANN001
    """Per-character isolation: one character's clip must not silence
    another character's feed."""
    await repository.add(_record(character_id="aiko", record_id="p1"))

    assert await repository.has_pending_for_character("hikari") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [PENDING_VIDEO_STATUS_PUBLISHED, PENDING_VIDEO_STATUS_DEGRADED],
)
async def test_a_terminal_row_is_no_longer_in_flight(
    repository,  # noqa: ANN001
    terminal_status: str,
) -> None:
    """Both exits release the gate — the published clip and the degraded
    picture are equally "the post arrived"."""
    await repository.add(_record(character_id="aiko", record_id="p1"))
    assert await repository.finish_if_pending(
        "p1", status=terminal_status, now=_NOW,
    ) is True

    assert await repository.has_pending_for_character("aiko") is False
