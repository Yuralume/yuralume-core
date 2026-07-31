"""HTTP-bounded, feedparser-backed RSS adapter.

Transport is owned here rather than delegated to ``feedparser.parse(url)`` so
HTTP status, TLS, redirects, response size and retry policy are explicit. The
parser only receives verified response bytes: an HTML error page can no longer
masquerade as malformed XML.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit

import feedparser
import httpx

from kokoro_link.contracts.rss_feed_fetcher import RawWorldEvent, RssFetchError

logger = logging.getLogger(__name__)

_TAG_STRIPPER = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_DEFAULT_RETRY_ATTEMPTS = 3
_DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
_MAX_REDIRECTS = 5
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})
_REQUEST_HEADERS = {
    "User-Agent": "Yuralume-RSS/1.0",
    "Accept": (
        "application/atom+xml, application/rss+xml, "
        "application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1"
    ),
}


class FeedparserRssAdapter:
    """Production RSS fetcher with bounded, observable degradation.

    Feedparser remains tolerant of malformed feeds. Individual invalid entries
    are dropped, while partial parses and drop counts are logged by source id.
    URLs and entry content never enter diagnostics.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = _DEFAULT_RETRY_BACKOFF_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = max(1.0, float(timeout_seconds))
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_backoff = max(0.0, float(retry_backoff_seconds))
        self._max_response_bytes = max(1, int(max_response_bytes))
        self._transport = transport

    async def fetch(
        self,
        *,
        source_id: str,
        source_name: str,
        feed_url: str,
        category: str,
        locale: str | None = None,
    ) -> list[RawWorldEvent]:
        _validate_feed_url(feed_url)
        content, content_type = await self._fetch_bytes(
            source_id=source_id,
            feed_url=feed_url,
        )
        try:
            parsed = await asyncio.wait_for(
                asyncio.to_thread(feedparser.parse, content),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RssFetchError(
                "parse_timeout",
                f"feed parsing exceeded {self._timeout:.0f}s",
            ) from exc

        bozo_exc = getattr(parsed, "bozo_exception", None)
        entries = list(parsed.entries or [])
        if parsed.bozo and bozo_exc and not entries:
            raise RssFetchError(
                "malformed_feed",
                f"parser rejected feed ({type(bozo_exc).__name__})",
            )
        if not entries and not getattr(parsed, "version", ""):
            raise RssFetchError(
                "invalid_feed",
                "response is not a recognizable RSS or Atom document",
            )
        if parsed.bozo and bozo_exc:
            logger.warning(
                "rss feed partially parsed source_id=%s error_type=%s entries=%d",
                source_id,
                type(bozo_exc).__name__,
                len(entries),
            )
        if not entries:
            logger.warning("rss feed contained no entries source_id=%s", source_id)
        if content_type.startswith("text/html"):
            logger.warning(
                "rss feed used HTML content type source_id=%s entries=%d",
                source_id,
                len(entries),
            )

        events: list[RawWorldEvent] = []
        for entry in entries:
            event = _entry_to_raw(
                entry,
                source_id=source_id,
                source_name=source_name,
                category=category,
                locale=locale,
            )
            if event is not None:
                events.append(event)
        dropped = len(entries) - len(events)
        if dropped:
            logger.warning(
                "rss feed dropped invalid entries source_id=%s count=%d",
                source_id,
                dropped,
            )
        if entries and not events:
            raise RssFetchError(
                "malformed_feed" if parsed.bozo else "invalid_entries",
                "feed contained no usable entries",
            )
        return events

    async def _fetch_bytes(
        self,
        *,
        source_id: str,
        feed_url: str,
    ) -> tuple[bytes, str]:
        timeout = httpx.Timeout(
            self._timeout,
            connect=min(5.0, self._timeout),
        )
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            headers=_REQUEST_HEADERS,
            transport=self._transport,
        ) as client:
            last_error: RssFetchError | None = None
            for attempt in range(1, self._retry_attempts + 1):
                try:
                    async with asyncio.timeout(self._timeout):
                        return await self._request_once(client, feed_url)
                except TimeoutError:
                    exc = RssFetchError(
                        "timeout",
                        "upstream request timed out",
                        retryable=True,
                    )
                    last_error = exc
                    if attempt >= self._retry_attempts:
                        raise exc
                    logger.warning(
                        "rss fetch retry source_id=%s attempt=%d/%d "
                        "error_code=%s",
                        source_id,
                        attempt,
                        self._retry_attempts,
                        exc.code,
                    )
                    if self._retry_backoff:
                        await asyncio.sleep(
                            self._retry_backoff * (2 ** (attempt - 1)),
                        )
                except RssFetchError as exc:
                    last_error = exc
                    if not exc.retryable or attempt >= self._retry_attempts:
                        raise
                    logger.warning(
                        "rss fetch retry source_id=%s attempt=%d/%d error_code=%s",
                        source_id,
                        attempt,
                        self._retry_attempts,
                        exc.code,
                    )
                    if self._retry_backoff:
                        await asyncio.sleep(
                            self._retry_backoff * (2 ** (attempt - 1)),
                        )
            assert last_error is not None
            raise last_error

    async def _request_once(
        self,
        client: httpx.AsyncClient,
        feed_url: str,
    ) -> tuple[bytes, str]:
        try:
            async with client.stream("GET", feed_url) as response:
                status = response.status_code
                if status < 200 or status >= 300:
                    raise RssFetchError(
                        "http_status",
                        f"upstream returned HTTP {status}",
                        retryable=(
                            status in _RETRYABLE_HTTP_STATUSES or status >= 500
                        ),
                        status_code=status,
                    )
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self._max_response_bytes:
                    raise _response_too_large(self._max_response_bytes)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise _response_too_large(self._max_response_bytes)
                return bytes(body), (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
        except RssFetchError:
            raise
        except httpx.TimeoutException as exc:
            raise RssFetchError(
                "timeout",
                "upstream request timed out",
                retryable=True,
            ) from exc
        except httpx.TooManyRedirects as exc:
            raise RssFetchError(
                "redirect_limit",
                f"upstream exceeded {_MAX_REDIRECTS} redirects",
            ) from exc
        except httpx.TransportError as exc:
            if _has_tls_verification_error(exc):
                raise RssFetchError(
                    "tls_certificate",
                    "upstream TLS certificate verification failed",
                ) from exc
            raise RssFetchError(
                "transport",
                "upstream connection failed",
                retryable=True,
            ) from exc


def _validate_feed_url(feed_url: str) -> None:
    parsed = urlsplit(feed_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RssFetchError(
            "invalid_url",
            "feed URL must use HTTP or HTTPS",
        )


def _response_too_large(max_bytes: int) -> RssFetchError:
    return RssFetchError(
        "response_too_large",
        f"feed response exceeded {max_bytes} bytes",
    )


def _has_tls_verification_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "certificate verify failed" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


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
        getattr(entry, "summary", "")
        or getattr(entry, "description", "")
        or ""
    )
    if len(summary) > 800:
        summary = summary[:800].rstrip() + "…"

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
        published_at=_published_at(entry),
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
                    struct.tm_year,
                    struct.tm_mon,
                    struct.tm_mday,
                    struct.tm_hour,
                    struct.tm_min,
                    struct.tm_sec,
                    tzinfo=timezone.utc,
                )
            except (ValueError, AttributeError):
                continue
    return datetime.now(timezone.utc)


def _clean_text(raw: str) -> str:
    no_tags = _TAG_STRIPPER.sub(" ", raw)
    return _WHITESPACE.sub(" ", no_tags).strip()
