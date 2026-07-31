"""AN1 — per-tenant cache and fail-soft rules behind the in-game notice dot."""

from __future__ import annotations

import pytest

from kokoro_link.application.services.cloud_announcement_service import (
    CloudAnnouncementService,
)
from kokoro_link.contracts.cloud_announcements import (
    AnnouncementUnread,
    AnnouncementUnreadUnavailable,
)


class _StubClient:
    def __init__(self, *answers: AnnouncementUnread | Exception) -> None:
        self._answers = list(answers)
        self.calls = 0

    async def fetch(self, tenant_id: str) -> AnnouncementUnread:
        self.calls += 1
        answer = self._answers[min(self.calls - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _unread(count: int = 1) -> AnnouncementUnread:
    return AnnouncementUnread(
        has_unread=count > 0,
        unread_count=count,
        latest_published_at="2026-07-29T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_serves_a_warm_cache_without_calling_upstream() -> None:
    client = _StubClient(_unread())
    service = CloudAnnouncementService(client=client, time_source=_Clock())

    first = await service.get_unread("tenant-1")
    second = await service.get_unread("tenant-1")

    assert first == second
    assert client.calls == 1


@pytest.mark.asyncio
async def test_refetches_after_the_ttl() -> None:
    clock = _Clock()
    client = _StubClient(_unread(1), _unread(3))
    service = CloudAnnouncementService(
        client=client, ttl_seconds=120.0, time_source=clock,
    )

    await service.get_unread("tenant-1")
    clock.now += 121.0
    again = await service.get_unread("tenant-1")

    assert client.calls == 2
    assert again is not None
    assert again.unread_count == 3


@pytest.mark.asyncio
async def test_refresh_bypasses_a_warm_cache() -> None:
    client = _StubClient(_unread(1), _unread(2))
    service = CloudAnnouncementService(client=client, time_source=_Clock())

    await service.get_unread("tenant-1")
    refreshed = await service.get_unread("tenant-1", refresh=True)

    assert client.calls == 2
    assert refreshed is not None
    assert refreshed.unread_count == 2


@pytest.mark.asyncio
async def test_an_outage_serves_last_known_good_marked_stale() -> None:
    clock = _Clock()
    client = _StubClient(
        _unread(2), AnnouncementUnreadUnavailable("upstream down"),
    )
    service = CloudAnnouncementService(
        client=client, ttl_seconds=120.0, time_source=clock,
    )

    await service.get_unread("tenant-1")
    clock.now += 121.0
    degraded = await service.get_unread("tenant-1")

    assert degraded is not None
    assert degraded.stale is True
    # The dot keeps pointing at the notice that was there a moment ago.
    assert degraded.has_unread is True
    assert degraded.unread_count == 2


@pytest.mark.asyncio
async def test_an_outage_with_nothing_cached_is_unknown_not_empty() -> None:
    service = CloudAnnouncementService(
        client=_StubClient(AnnouncementUnreadUnavailable("upstream down")),
        time_source=_Clock(),
    )

    # None (→ 503 → dot unchanged), never a fabricated "nothing unread", which
    # would hide a notice the operator was required to publish.
    assert await service.get_unread("tenant-1") is None


@pytest.mark.asyncio
async def test_tenants_do_not_share_a_cache_entry() -> None:
    client = _StubClient(_unread(1), _unread(5))
    service = CloudAnnouncementService(client=client, time_source=_Clock())

    first = await service.get_unread("tenant-1")
    second = await service.get_unread("tenant-2")

    assert first is not None and first.unread_count == 1
    assert second is not None and second.unread_count == 5
    assert client.calls == 2


@pytest.mark.asyncio
async def test_a_blank_tenant_never_reaches_upstream() -> None:
    client = _StubClient(_unread())
    service = CloudAnnouncementService(client=client, time_source=_Clock())

    assert await service.get_unread("   ") is None
    assert client.calls == 0
