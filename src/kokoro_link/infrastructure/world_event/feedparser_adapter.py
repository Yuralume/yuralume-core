"""Feedparser-backed RSS adapter.

The ingestion service owns retry / scheduling / dedup; this adapter
just turns one feed URL into ``RawWorldEvent``s. Network calls are
offloaded to a thread because ``feedparser.parse`` is sync.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import feedparser

from kokoro_link.contracts.rss_feed_fetcher import RawWorldEvent

logger = logging.getLogger(__name__)

_TAG_STRIPPER = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


class FeedparserRssAdapter:
    """Production RSS fetcher.

    Trusts feedparser's tolerance of malformed feeds — entries that
    fail individual parsing are dropped, not propagated, so a single
    broken item in a big feed doesn't lose the whole batch.
    """

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = max(1.0, float(timeout_seconds))

    async def fetch(
        self,
        *,
        source_id: str,
        source_name: str,
        feed_url: str,
        category: str,
        locale: str | None = None,
    ) -> list[RawWorldEvent]:
        # ``feedparser.parse`` does its own HTTP — push to a thread to
        # avoid blocking the event loop.
        try:
            parsed = await asyncio.wait_for(
                asyncio.to_thread(feedparser.parse, feed_url),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"feedparser timeout after {self._timeout:.0f}s",
            ) from exc

        bozo_exc = getattr(parsed, "bozo_exception", None)
        if parsed.bozo and bozo_exc and not parsed.entries:
            # Hard parse error and zero entries — propagate so the
            # ingest service marks the source unhealthy.
            raise RuntimeError(f"feed parse error: {bozo_exc!r}")

        events: list[RawWorldEvent] = []
        for entry in parsed.entries or []:
            event = _entry_to_raw(
                entry,
                source_id=source_id,
                source_name=source_name,
                category=category,
                locale=locale,
            )
            if event is not None:
                events.append(event)
        return events


def _entry_to_raw(
    entry,
    *,
    source_id: str,
    source_name: str,
    category: str,
    locale: str | None,
) -> RawWorldEvent | None:
    title = _clean_text(getattr(entry, "title", "") or "")
    link = (getattr(entry, "link", "") or "").strip()
    if not title or not link:
        return None

    summary = _clean_text(
        getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    )
    # Cap summary so a single 50KB entry doesn't blow up the embedder
    # call or the prompt window.
    if len(summary) > 800:
        summary = summary[:800].rstrip() + "…"

    published = _published_at(entry)

    tags_raw = getattr(entry, "tags", None) or []
    topic_tags: list[str] = []
    for tag in tags_raw:
        term = getattr(tag, "term", None)
        if term and isinstance(term, str):
            cleaned = term.strip()
            if cleaned and cleaned not in topic_tags:
                topic_tags.append(cleaned)

    return RawWorldEvent(
        source_id=source_id,
        source_name=source_name,
        title=title,
        summary=summary,
        url=link,
        published_at=published,
        category=category,
        locale=(locale or None),
        topic_tags=tuple(topic_tags),
    )


def _published_at(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = getattr(entry, field, None)
        if struct is not None:
            try:
                return datetime(
                    struct.tm_year, struct.tm_mon, struct.tm_mday,
                    struct.tm_hour, struct.tm_min, struct.tm_sec,
                    tzinfo=timezone.utc,
                )
            except (ValueError, AttributeError):
                continue
    # Feedparser strips publish info — fall back to "now" so the row
    # is still ordered sensibly. Curator filters by ``published_at``
    # window so downstream stays consistent.
    return datetime.now(timezone.utc)


def _clean_text(raw: str) -> str:
    no_tags = _TAG_STRIPPER.sub(" ", raw)
    return _WHITESPACE.sub(" ", no_tags).strip()
