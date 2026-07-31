"""In-process per-tenant cache for the hosted notice board's unread flag.

Sibling of :class:`CloudCreditService`, with one deliberate difference: the TTL
is far longer (120s vs 20s). The balance badge has to feel live because the
player just changed it themselves; a notice was published by an operator hours
or days ago, and nothing the player does in-game can change whether one is
waiting. Polling harder would only add load to answer the same bit.

Fail-soft is explicit, matching the badge next to it: an outage serves the
previous value flagged ``stale``, and never having had one returns ``None`` so
the dot stays hidden. Inventing a ``False`` would quietly hide a notice the
operator is required to have given.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from kokoro_link.contracts.cloud_announcements import (
    AnnouncementUnread,
    AnnouncementUnreadPort,
    AnnouncementUnreadUnavailable,
)

_DEFAULT_TTL_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class CloudAnnouncementSnapshot:
    """An unread standing plus how much it can be trusted."""

    has_unread: bool
    unread_count: int
    latest_published_at: str | None
    stale: bool
    """True when the upstream read failed and this is last-known-good."""


class CloudAnnouncementService:
    def __init__(
        self,
        *,
        client: AnnouncementUnreadPort,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = max(1.0, ttl_seconds)
        self._now = time_source
        self._cache: dict[str, AnnouncementUnread] = {}
        self._fetched_at: dict[str, float] = {}

    async def get_unread(
        self, tenant_id: str, *, refresh: bool = False,
    ) -> CloudAnnouncementSnapshot | None:
        """Return the tenant's unread standing, or ``None`` when nothing is known.

        ``refresh`` bypasses a warm cache — used when the player returns from
        the Portal, where they may have just read the board."""
        key = (tenant_id or "").strip()
        if not key:
            return None
        if not refresh and key in self._cache and not self._expired(key):
            return _snapshot(self._cache[key], stale=False)
        try:
            unread = await self._client.fetch(key)
        except AnnouncementUnreadUnavailable:
            cached = self._cache.get(key)
            return None if cached is None else _snapshot(cached, stale=True)
        self._store(key, unread)
        return _snapshot(unread, stale=False)

    def _expired(self, key: str) -> bool:
        return (self._now() - self._fetched_at.get(key, 0.0)) > self._ttl

    def _store(self, key: str, unread: AnnouncementUnread) -> None:
        self._cache[key] = unread
        self._fetched_at[key] = self._now()


def _snapshot(
    unread: AnnouncementUnread, *, stale: bool,
) -> CloudAnnouncementSnapshot:
    return CloudAnnouncementSnapshot(
        has_unread=unread.has_unread,
        unread_count=unread.unread_count,
        latest_published_at=unread.latest_published_at,
        stale=stale,
    )
