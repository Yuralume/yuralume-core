"""Propagate an admin ``region`` edit onto already-ingested world events.

``world_events.source_region`` is denormalised from ``rss_sources.region``
at ingest time so the curator can narrow the pool in SQL without a join.
The cost of that copy is that editing the registry alone changes nothing
for events already in the pool: they keep leaking into the old region (or
stay hidden from every region) until GC evicts them.

There is no foreign key to walk back — the pool only carries the source's
display *name*, copied onto each row. This service re-tags on that name and
**refuses** whenever it cannot prove the name identifies exactly one
registry entry, because a wrong join would move a different feed's events
into the wrong region, which is the very failure being fixed.

Whatever it declines to touch is not lost: ``RssIngestionService`` repairs
the region of every event it re-sees on the next pass over the feed.
"""

from __future__ import annotations

import logging

from kokoro_link.contracts.rss_source import RssSourceRepositoryPort
from kokoro_link.contracts.world_event import WorldEventRepositoryPort

logger = logging.getLogger(__name__)


class RssSourceRegionRetagService:
    def __init__(
        self,
        *,
        rss_source_repository: RssSourceRepositoryPort,
        world_event_repository: WorldEventRepositoryPort,
    ) -> None:
        self._sources = rss_source_repository
        self._events = world_event_repository

    async def retag(
        self, *, ingested_source_name: str, region: str | None,
    ) -> int:
        """Move every event ingested under ``ingested_source_name`` to
        ``region``. Returns the number of events actually changed.

        ``ingested_source_name`` is the name the events were *written*
        with, which is the source's stored name before the current edit —
        a rename in the same request does not retroactively rename rows.
        """
        name = (ingested_source_name or "").strip()
        if not name:
            return 0
        if not await self._name_is_unambiguous(name):
            logger.info(
                "skipping world_event region retag: source name is shared",
                extra={"source_name": name},
            )
            return 0
        return await self._events.retag_source_region(
            source_name=name, source_region=region,
        )

    async def _name_is_unambiguous(self, name: str) -> bool:
        sources = await self._sources.list_all()
        return sum(1 for source in sources if source.name == name) <= 1
