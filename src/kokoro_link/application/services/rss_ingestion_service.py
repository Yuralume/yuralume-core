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

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.rss_feed_fetcher import (
    RawWorldEvent,
    RssFeedFetcherPort,
    RssFetchError,
)
from kokoro_link.contracts.rss_source import RssSourceRepositoryPort
from kokoro_link.contracts.character_event_mention import (
    CharacterEventMentionRepositoryPort,
)
from kokoro_link.contracts.world_event import WorldEventRepositoryPort
from kokoro_link.domain.entities.rss_source import RssSource
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
    events_failed_persist: int = 0
    """Parsed events that could not be persisted. Kept separate from fetch
    failures so a source transport can be healthy while storage is degraded."""
    embed_batches_failed: int = 0
    """Operational embedder batches that failed and degraded to null vectors."""


@dataclass(frozen=True, slots=True)
class _SourceIngestionResult:
    persisted: int = 0
    skipped_dedup: int = 0
    skipped_embed: int = 0
    region_corrected: int = 0
    failed_persist: int = 0
    embed_batches_failed: int = 0
    error: str | None = None


class RssIngestionService:
    def __init__(
        self,
        *,
        rss_source_repository: RssSourceRepositoryPort,
        world_event_repository: WorldEventRepositoryPort,
        feed_fetcher: RssFeedFetcherPort,
        embedder: EmbedderPort,
        max_age_days: int = 7,
        event_mention_repository: (
            "CharacterEventMentionRepositoryPort | None"
        ) = None,
    ) -> None:
        self._sources = rss_source_repository
        self._events = world_event_repository
        self._fetcher = feed_fetcher
        self._embedder = embedder
        self._max_age_days = max_age_days
        # Mentions point into the pool this service prunes. PostgreSQL
        # cascades them away with the event; self-host SQLite does not
        # enforce foreign keys at all, so the same sweep has to say so
        # explicitly or the table only ever grows there.
        self._event_mentions = event_mention_repository

    async def ingest_all(self) -> IngestionReport:
        sources = await self._sources.list_all(enabled_only=True)
        attempted = 0
        succeeded = 0
        persisted = 0
        skipped_dedup = 0
        skipped_embed = 0
        region_corrected = 0
        failed_persist = 0
        embed_batches_failed = 0
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
                safe_error = _safe_fetch_error(exc)
                errors.append(f"{source.id}: {safe_error}")
                logger.warning(
                    "rss source fetch failed source_id=%s error_code=%s "
                    "error_type=%s",
                    source.id,
                    exc.code if isinstance(exc, RssFetchError)
                    else "unexpected_fetch_error",
                    type(exc).__name__,
                )
                await self._mark_source_error(
                    source_id=source.id, at=now, error=safe_error,
                )
                continue

            try:
                result = await self._ingest_source(
                    source=source,
                    raws=raws,
                    now=now,
                    cutoff_age=cutoff_age,
                )
            except Exception as exc:  # fail-soft beyond transport, too
                safe_error = f"source_processing_error: {type(exc).__name__}"
                errors.append(f"{source.id}: {safe_error}")
                logger.warning(
                    "rss source processing failed source_id=%s error_type=%s",
                    source.id,
                    type(exc).__name__,
                )
                await self._mark_source_error(
                    source_id=source.id, at=now, error=safe_error,
                )
                continue

            persisted += result.persisted
            skipped_dedup += result.skipped_dedup
            skipped_embed += result.skipped_embed
            region_corrected += result.region_corrected
            failed_persist += result.failed_persist
            embed_batches_failed += result.embed_batches_failed
            if result.error is not None:
                errors.append(f"{source.id}: {result.error}")
            else:
                succeeded += 1

        return IngestionReport(
            sources_attempted=attempted,
            sources_succeeded=succeeded,
            events_persisted=persisted,
            events_skipped_dedup=skipped_dedup,
            events_skipped_embed=skipped_embed,
            errors=tuple(errors),
            events_region_corrected=region_corrected,
            events_failed_persist=failed_persist,
            embed_batches_failed=embed_batches_failed,
        )

    async def _ingest_source(
        self,
        *,
        source: RssSource,
        raws: list[RawWorldEvent],
        now: datetime,
        cutoff_age: float,
    ) -> _SourceIngestionResult:
        """Process one fetched source; callers isolate any unexpected failure."""
        skipped_dedup = 0
        skipped_embed = 0
        failed_persist = 0
        embed_batches_failed = 0
        persisted = 0
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

        region_corrected = await self._heal_source_region(
            source_id=source.id, urls=known_urls, region=source.region,
        )

        embeddings: list[tuple[float, ...] | None]
        if new_events and self._embedder.is_operational:
            try:
                embeddings = await self._embedder.embed_many(
                    [_embed_text(event) for event in new_events]
                )
            except EmbedderError as exc:
                embed_batches_failed += 1
                logger.warning(
                    "rss embed batch failed source_id=%s error_type=%s; "
                    "persisting null vectors",
                    source.id,
                    type(exc).__name__,
                )
                embeddings = [None] * len(new_events)
        else:
            embeddings = [None] * len(new_events)

        if len(embeddings) != len(new_events):
            embed_batches_failed += 1
            logger.warning(
                "rss embed batch length mismatch source_id=%s expected=%d "
                "actual=%d; persisting missing vectors as null",
                source.id,
                len(new_events),
                len(embeddings),
            )
            embeddings = (
                list(embeddings[:len(new_events)])
                + [None] * max(0, len(new_events) - len(embeddings))
            )

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
                source_region=source.region,
                topic_tags=raw.topic_tags,
                embedding=list(vec) if vec is not None else None,
            )
            try:
                await self._events.upsert(event)
                persisted += 1
            except Exception as exc:
                failed_persist += 1
                logger.warning(
                    "world_event upsert failed source_id=%s error_type=%s",
                    source.id,
                    type(exc).__name__,
                )

        storage_error = None
        if failed_persist:
            storage_error = (
                "storage_degraded: "
                f"persisted={persisted} failed={failed_persist}"
            )
            await self._mark_source_error(
                source_id=source.id,
                at=now,
                error=storage_error,
                fetched_count=persisted,
            )
        else:
            await self._sources.mark_success(
                source.id, at=now, fetched_count=persisted,
            )
        return _SourceIngestionResult(
            persisted=persisted,
            skipped_dedup=skipped_dedup,
            skipped_embed=skipped_embed,
            region_corrected=region_corrected,
            failed_persist=failed_persist,
            embed_batches_failed=embed_batches_failed,
            error=storage_error,
        )

    async def _mark_source_error(
        self,
        *,
        source_id: str,
        at: datetime,
        error: str,
        fetched_count: int = 0,
    ) -> None:
        """Record source health without letting health persistence abort a pass."""
        try:
            await self._sources.mark_error(
                source_id,
                at=at,
                error=error,
                fetched_count=fetched_count,
            )
        except Exception as exc:
            logger.warning(
                "rss source error status write failed source_id=%s error_type=%s",
                source_id,
                type(exc).__name__,
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
                "world_event source_region repair failed source_id=%s "
                "url_count=%d error_type=%s",
                source_id,
                len(urls),
                type(exc).__name__,
            )
            return 0

    async def gc(self) -> int:
        """Delete events older than the retention window. The curator
        already filters out stale events from prompt injection, but
        the table grows unbounded if nothing prunes; running GC after
        each ingest pass keeps it small.

        Returns the number of *events* deleted — the mention sweep rides
        along on the same cutoff (a mention of an event nobody can look
        up is dead weight) and is best-effort: losing it costs disk, and
        must never take the event GC down with it."""
        cutoff = datetime.now(timezone.utc) - _days(self._max_age_days * 2)
        deleted = await self._events.delete_older_than(cutoff)
        if self._event_mentions is not None:
            try:
                await self._event_mentions.delete_older_than(cutoff)
            except Exception:
                logger.exception("world-event mention gc failed")
        return deleted


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


def _safe_fetch_error(exc: Exception) -> str:
    """Return a stable diagnostic that cannot contain a configured URL.

    Typed adapter failures already carry only an error code and bounded safe
    detail. Unknown fetchers may put credentials or full URLs in exception
    messages, so retain only their class name.
    """
    if isinstance(exc, RssFetchError):
        return str(exc)
    return f"unexpected_fetch_error: {type(exc).__name__}"


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)
