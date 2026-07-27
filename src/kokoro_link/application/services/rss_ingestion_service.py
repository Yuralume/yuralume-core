"""Orchestrate RSS ingestion across configured sources.

The service runs a single pass over every enabled ``RssSource``:

1. Ask the fetcher port for raw events
2. Skip URLs already present in ``world_events`` (repairing their
   denormalised ``source_region`` on the way past)
3. Embed (best-effort — failure → null embedding, curator skips later)
4. Upsert into ``world_events``
5. Mark source success / error

Per-source failures are isolated; a dead feed updates ``last_error``
but does not abort the batch. The scheduler calls ``ingest_all`` every
N minutes (see ``WorldEventScheduler``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.rss_feed_fetcher import (
    RawWorldEvent,
    RssFeedFetcherPort,
)
from kokoro_link.contracts.rss_source import RssSourceRepositoryPort
from kokoro_link.contracts.world_event import WorldEventRepositoryPort
from kokoro_link.domain.entities.world_event import WorldEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """Summary returned by ``ingest_all`` — useful for tests / admin."""

    sources_attempted: int
    sources_succeeded: int
    events_persisted: int
    events_skipped_dedup: int
    events_skipped_embed: int
    errors: tuple[str, ...]
    events_region_corrected: int = 0
    """Already-ingested events whose denormalised ``source_region`` was
    brought back in line with the registry during this pass."""


class RssIngestionService:
    def __init__(
        self,
        *,
        rss_source_repository: RssSourceRepositoryPort,
        world_event_repository: WorldEventRepositoryPort,
        feed_fetcher: RssFeedFetcherPort,
        embedder: EmbedderPort,
        max_age_days: int = 7,
    ) -> None:
        self._sources = rss_source_repository
        self._events = world_event_repository
        self._fetcher = feed_fetcher
        self._embedder = embedder
        self._max_age_days = max_age_days

    async def ingest_all(self) -> IngestionReport:
        sources = await self._sources.list_all(enabled_only=True)
        attempted = 0
        succeeded = 0
        persisted = 0
        skipped_dedup = 0
        skipped_embed = 0
        region_corrected = 0
        errors: list[str] = []

        cutoff = datetime.now(timezone.utc).replace(microsecond=0)
        cutoff_age = cutoff.timestamp() - self._max_age_days * 86400

        for source in sources:
            attempted += 1
            now = datetime.now(timezone.utc)
            try:
                raws = await self._fetcher.fetch(
                    source_id=source.id,
                    source_name=source.name,
                    feed_url=source.feed_url,
                    category=source.category,
                    locale=source.locale,
                )
            except Exception as exc:  # fail-soft per source
                errors.append(
                    f"{source.id} ({source.feed_url}): "
                    f"{type(exc).__name__}: {exc}"
                )
                logger.warning(
                    "rss source fetch failed",
                    extra={
                        "source_id": source.id,
                        "source_name": source.name,
                        "feed_url": source.feed_url,
                        "category": source.category,
                        "error_type": type(exc).__name__,
                        "error": repr(exc),
                    },
                )
                await self._sources.mark_error(
                    source.id, at=now, error=str(exc),
                )
                continue

            new_events: list[RawWorldEvent] = []
            known_urls: list[str] = []
            for raw in raws:
                raw = _with_trusted_published_at(raw, fetched_at=now)
                if raw.published_at.timestamp() < cutoff_age:
                    continue
                if await self._events.has_url(raw.url):
                    skipped_dedup += 1
                    known_urls.append(raw.url)
                    continue
                new_events.append(raw)

            region_corrected += await self._heal_source_region(
                source_id=source.id, urls=known_urls, region=source.region,
            )

            embeddings: list[tuple[float, ...] | None] = []
            if new_events and self._embedder.is_operational:
                try:
                    embeddings = await self._embedder.embed_many(
                        [_embed_text(e) for e in new_events]
                    )
                except EmbedderError as exc:
                    logger.warning(
                        "rss embed batch failed; persisting null vectors",
                        extra={"source_id": source.id, "error": repr(exc)},
                    )
                    embeddings = [None] * len(new_events)
            else:
                embeddings = [None] * len(new_events)

            for raw, vec in zip(new_events, embeddings, strict=False):
                if vec is None:
                    skipped_embed += 1
                event = WorldEvent(
                    id=str(uuid4()),
                    source=raw.source_name,
                    title=raw.title,
                    summary=raw.summary,
                    url=raw.url,
                    published_at=raw.published_at,
                    fetched_at=now,
                    category=raw.category or "news",
                    locale=raw.locale or source.locale or None,
                    # Region is a property of the *registry entry*, never of
                    # the feed payload — denormalised here so the curator can
                    # narrow the pool in SQL without joining rss_sources.
                    source_region=source.region,
                    topic_tags=raw.topic_tags,
                    embedding=list(vec) if vec is not None else None,
                )
                try:
                    await self._events.upsert(event)
                    persisted += 1
                except Exception as exc:
                    logger.warning(
                        "world_event upsert failed",
                        extra={"url": raw.url, "error": repr(exc)},
                    )

            await self._sources.mark_success(
                source.id, at=now, fetched_count=len(new_events),
            )
            succeeded += 1

        return IngestionReport(
            sources_attempted=attempted,
            sources_succeeded=succeeded,
            events_persisted=persisted,
            events_skipped_dedup=skipped_dedup,
            events_skipped_embed=skipped_embed,
            errors=tuple(errors),
            events_region_corrected=region_corrected,
        )

    async def _heal_source_region(
        self, *, source_id: str, urls: list[str], region: str | None,
    ) -> int:
        """Re-point de-duplicated events at the source's current region.

        ``source_region`` is denormalised onto the event at ingest time and
        the pool carries no back-link to ``rss_sources``, so an event the
        de-dup probe skipped keeps the region it was first written with.
        After a migration backfill or an admin re-tag that value is stale —
        the event leaks into the wrong region's pool, or hides from every
        pool — and nothing else would ever fix it before GC. Re-seeing the
        URL is the one moment the pipeline knows which source owns it.

        Fail-soft like the rest of the pass: a repair that fails must not
        cost the source its fresh events.
        """
        if not urls:
            return 0
        try:
            return await self._events.sync_source_region(
                urls=urls, source_region=region,
            )
        except Exception as exc:  # noqa: BLE001 — repair is best-effort
            logger.warning(
                "world_event source_region repair failed",
                extra={
                    "source_id": source_id,
                    "url_count": len(urls),
                    "error": repr(exc),
                },
            )
            return 0

    async def gc(self) -> int:
        """Delete events older than the retention window. The curator
        already filters out stale events from prompt injection, but
        the table grows unbounded if nothing prunes; running GC after
        each ingest pass keeps it small."""
        cutoff = datetime.now(timezone.utc) - _days(self._max_age_days * 2)
        return await self._events.delete_older_than(cutoff)


# A publish timestamp at or before this instant is a broken feed, not a
# real date: RSS producers that fail to emit ``pubDate`` routinely serialise
# the Unix epoch instead (observed across whole feeds, e.g. UDN's 即時 feed).
# Anything from before RSS itself existed is in the same class.
_IMPLAUSIBLE_PUBLISHED_BEFORE = datetime(1995, 1, 1, tzinfo=timezone.utc)

# Feeds also emit dates far in the future (timezone maths bugs, templating
# accidents). Those would pin an item to the top of every recency sort
# forever, so they are treated as equally untrustworthy.
_FUTURE_PUBLISHED_TOLERANCE_SECONDS = 2 * 86400


def _with_trusted_published_at(
    raw: RawWorldEvent, *, fetched_at: datetime,
) -> RawWorldEvent:
    """Replace an implausible ``published_at`` with the fetch time.

    A general rule, deliberately not a per-source workaround: any feed
    whose item dates are unusable gets the same treatment. Without it an
    epoch-0 item is silently dropped by the freshness cutoff (the whole
    feed disappears), and a far-future item would monopolise every
    recency-ordered read.
    """
    published = raw.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    too_old = published <= _IMPLAUSIBLE_PUBLISHED_BEFORE
    too_new = (
        published.timestamp()
        > fetched_at.timestamp() + _FUTURE_PUBLISHED_TOLERANCE_SECONDS
    )
    if not too_old and not too_new:
        return raw if published is raw.published_at else replace(
            raw, published_at=published,
        )
    logger.debug(
        "rss entry published_at implausible; using fetch time",
        extra={
            "source_id": raw.source_id,
            "url": raw.url,
            "published_at": published.isoformat(),
        },
    )
    return replace(raw, published_at=fetched_at)


def _embed_text(raw: RawWorldEvent) -> str:
    """Compose the embedding input for a raw event.

    Title carries most of the topical signal; summary adds nuance for
    fine-grained matching. Source name and tags concatenated in case
    the model relies on them for domain context."""
    parts = [raw.title]
    if raw.summary:
        parts.append(raw.summary)
    if raw.topic_tags:
        parts.append("tags: " + ", ".join(raw.topic_tags))
    parts.append(f"source: {raw.source_name} ({raw.category})")
    return "\n".join(parts)


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)
