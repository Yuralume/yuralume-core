"""HTTP reader + in-process cache for the Cloud official-card catalog.

Two classes, two jobs:

* :class:`OfficialCardCatalogClient` speaks HTTP and nothing else. Anonymous
  ``GET``s, a short timeout, and a hard refusal to guess: anything that is
  not a well-formed answer raises
  :class:`OfficialCardCatalogUnavailable`.
* :class:`CachedOfficialCardCatalog` decides what to do about that. It holds
  a TTL cache and serves **stale on error**, because a catalog that was
  correct ten minutes ago is a far better answer for a player browsing
  characters than an empty shelf.

Neither of them runs at boot. The first player who opens the card gallery
is what triggers the first request (plan §3.5: 啟動不打 Cloud), so a Cloud
outage can never delay or fail a deployment's startup.

**Nothing identifying is sent.** No credential, no deployment id, no
version, no telemetry. A self-hoster reading the official catalog is a
stranger asking a public question, and this file is where that promise is
either kept or quietly broken.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from kokoro_link.contracts.official_card_catalog import (
    OfficialCardCatalogPort,
    OfficialCardCatalogUnavailable,
    OfficialCardDetail,
    OfficialCardImage,
    OfficialCardNotFound,
    OfficialCardSummary,
)

_LOGGER = logging.getLogger(__name__)

CATALOG_SCHEMA_VERSION = 1
"""The document shape this build knows how to read.

A newer ``schema_version`` from Cloud means a consumer would have to change
to keep reading — so this build stops reading rather than half-understanding
a document and rendering a mangled card. The catalog then behaves exactly as
if Cloud were unreachable: stale content, or an empty official shelf.
"""

_PUBLIC_BASE_PATH = "/v1/public/official-cards"
"""The path the catalog is mounted at on the Cloud side.

Stripping it from the configured URL is how the adapter recovers the *mount
prefix* — ``https://app.yuralume.com/api/user`` for the default edge, where
the reverse proxy adds ``/api/user`` in front of the service's own paths.
Asset links in the documents are service-relative (``/v1/public/...``), so
joining them to the origin alone would drop that prefix and 404 every image.
"""

_DEFAULT_TIMEOUT_SECONDS = 5.0
"""Short on purpose. httpx applies this per socket operation, not to the
whole transfer, so a multi-megabyte ``.lumecard`` still downloads fine as
long as bytes keep arriving — while a Cloud that has stopped answering
costs a browsing player five seconds, once, before the shelf falls back to
cache."""

_DEFAULT_CACHE_TTL_SECONDS = 600.0

ARTIFACT_CHUNK_BYTES = 64 * 1024
"""How much of an artifact download may arrive between two size checks.

Stated rather than left to httpx, because the default is "whatever the
transport handed over" — and for a ``Content-Encoding: gzip`` body that is a
*decompressed* block whose size the sender chooses. Pinning it makes the
overshoot past :data:`MAX_ARTIFACT_BYTES`, and therefore the amount this
process retains, one chunk in every case.

It does **not** on its own bound the decompression itself:
``Response.aiter_bytes`` decodes a whole transport read before chunking it,
so the transient buffer would still be the sender's number. That half is
closed by asking for no encoding at all — see
:meth:`OfficialCardCatalogClient._stream`. The two together are what make
"we never hold more than the cap plus a chunk" true rather than nearly
true."""

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
"""Matched to the importer's own uncompressed-size cap.

A real official card is single-digit megabytes; this exists so a download
that is not what we asked for cannot be held in memory while the zip guard
downstream works that out. Counted **as the bytes arrive** (see
:meth:`OfficialCardCatalogClient.fetch_artifact`) — measuring a body that
has already been read would make this a description of the allocation
rather than a limit on it."""


class OfficialCardCatalogClient:
    """Anonymous reads of the public catalog endpoints.

    ``transport`` exists for tests (``httpx.MockTransport``); production
    leaves it ``None`` and lets httpx build the real one per call, matching
    the other Cloud clients in this package.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._asset_origin = _asset_origin(self._base_url)

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def fetch_catalog(self, *, locale: str) -> list[OfficialCardSummary]:
        payload = await self._get_json(self._base_url, params={"locale": locale})
        raw_cards = payload.get("cards")
        if not isinstance(raw_cards, list):
            raise OfficialCardCatalogUnavailable(
                "official card catalog response has no card list",
            )
        cards: list[OfficialCardSummary] = []
        for entry in raw_cards:
            summary = self._as_summary(entry)
            if summary is not None:
                cards.append(summary)
        return cards

    async def fetch_card(
        self, card_id: str, *, locale: str,
    ) -> OfficialCardDetail:
        payload = await self._get_json(
            self._card_url(card_id),
            params={"locale": locale},
            missing_is_not_found=True,
        )
        raw_card = payload.get("card")
        if not isinstance(raw_card, Mapping):
            raise OfficialCardCatalogUnavailable(
                "official card detail response has no card",
            )
        profile = raw_card.get("profile")
        return OfficialCardDetail(
            id=_text(raw_card.get("id")) or card_id,
            title=_text(raw_card.get("title")),
            description=_text(raw_card.get("description")),
            tags=_text_tuple(raw_card.get("tags")),
            author=_text(raw_card.get("author")),
            note=_text(raw_card.get("note")),
            localized=payload.get("localized") is True,
            # Top-level beside ``localized``, and read the same way: only a
            # literal ``true`` is a warning. A Cloud that predates the field
            # is not claiming freshness, but it is also not claiming drift,
            # and the install path must stay usable against it.
            stale=payload.get("stale") is True,
            locale=_text(payload.get("locale")),
            available_locales=_text_tuple(payload.get("available_locales")),
            # Passed through as-is: the field set belongs to the card
            # translator on both sides of the wire, not to this adapter.
            profile=dict(profile) if isinstance(profile, Mapping) else {},
            images=self._as_images(raw_card.get("images")),
        )

    async def fetch_artifact(self, card_id: str) -> bytes:
        """Download one ``.lumecard``, counting as it arrives.

        Streamed rather than read whole, because the cap has to be enforced
        *while* the bytes come in. Reading the body first and measuring
        afterwards makes the limit a description of what already happened —
        a redirect to something enormous, or a compromised catalog, would
        have been fully resident in this process before anybody objected,
        on a path every logged-in player can trigger. Here the connection is
        dropped the moment the count crosses the line, and a body that
        declares no ``Content-Length`` is bounded exactly the same way.

        The chunk size is pinned (:data:`ARTIFACT_CHUNK_BYTES`) so that "the
        moment the count crosses the line" is a fixed amount of overshoot.
        Left to the transport it is one decoded block, which for a gzipped
        body is a number the *sender* picks.
        """
        url = f"{self._card_url(card_id)}/artifact"
        chunks: list[bytes] = []
        received = 0
        async with self._stream(url) as response:
            async for chunk in response.aiter_bytes(
                chunk_size=ARTIFACT_CHUNK_BYTES,
            ):
                received += len(chunk)
                if received > MAX_ARTIFACT_BYTES:
                    # Leaving the ``async with`` closes the response, which
                    # is what actually hangs up on the sender.
                    raise OfficialCardCatalogUnavailable(
                        f"official card artifact is larger than "
                        f"{MAX_ARTIFACT_BYTES} bytes",
                    )
                chunks.append(chunk)
        if not received:
            raise OfficialCardCatalogUnavailable(
                "official card artifact response is empty",
            )
        return b"".join(chunks)

    # -- internals ---------------------------------------------------------- #

    @asynccontextmanager
    async def _stream(self, url: str) -> AsyncIterator[httpx.Response]:
        """A started response whose body has not been read yet.

        Mirrors :meth:`_get`'s failure vocabulary exactly — an unreachable
        catalog, a typo'd URL and a 404 mean the same things here as they do
        for the JSON documents — but hands back the response before the body
        arrives so the caller can decide how much of it to accept.
        """
        if not self._base_url:
            raise OfficialCardCatalogUnavailable(
                "official card catalog url is not configured",
            )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    # Same promise as every other request in this file: the
                    # headers set here are the only ones this client chooses,
                    # and none of them says anything about *this* deployment
                    # — no credential, no id, no version. (httpx's own
                    # transport defaults still ride along; see ``_get``.)
                    #
                    # `identity` is the size guard's other half. A
                    # `.lumecard` is a zip — compressing it again buys
                    # nothing — while accepting an encoding means httpx
                    # inflates each transport read *before* anything counts
                    # it, so a few kilobytes of crafted gzip becomes tens of
                    # megabytes resident on a path any logged-in player can
                    # trigger. Declining the encoding removes the multiplier
                    # instead of trying to survive it.
                    headers={
                        "Accept": "application/octet-stream",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code == 404:
                        raise OfficialCardNotFound(
                            f"official card {url} is not published",
                        )
                    if response.status_code >= 400:
                        raise OfficialCardCatalogUnavailable(
                            f"official card catalog returned "
                            f"{response.status_code}",
                        )
                    yield response
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise OfficialCardCatalogUnavailable(
                f"official card catalog unreachable: {exc}",
            ) from exc

    def _card_url(self, card_id: str) -> str:
        # ``quote`` with no safe characters: an id is one path segment and a
        # slash inside it must not silently become a different route.
        return f"{self._base_url}/{quote(card_id, safe='')}"

    async def _get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        missing_is_not_found: bool = False,
    ) -> httpx.Response:
        if not self._base_url:
            raise OfficialCardCatalogUnavailable(
                "official card catalog url is not configured",
            )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
                # Safe here precisely because the request carries nothing:
                # there is no credential a redirect could hand to somebody
                # else. Without this, a self-hoster whose edge normalises
                # http→https or a trailing slash would see an empty shelf and
                # no reason why.
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    url,
                    params=dict(params or {}),
                    # The only header this client sets. httpx still adds its
                    # own transport defaults (``User-Agent``, ``Accept-
                    # Encoding``, ``Connection``), which say "an httpx of
                    # roughly this version" and nothing about *this*
                    # deployment — and that is the line: nothing here
                    # identifies who is running Yuralume, and nothing may be
                    # added that would. No credential, no deployment id, no
                    # version, no install count.
                    headers={"Accept": "application/json"},
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # ``InvalidURL`` is not an ``HTTPError``: a self-hoster who typed
            # the catalog env wrong would otherwise raise straight through
            # the cache and turn the whole card gallery into a 500. A
            # misconfiguration is still just "no catalog".
            raise OfficialCardCatalogUnavailable(
                f"official card catalog unreachable: {exc}",
            ) from exc
        if response.status_code == 404 and missing_is_not_found:
            # The catalog answered, and this card is not in it — withdrawn,
            # never published, or a stale id. That is an answer, not an
            # outage: it must evict rather than fall back to stale.
            raise OfficialCardNotFound(
                f"official card {url} is not published",
            )
        if response.status_code >= 400:
            raise OfficialCardCatalogUnavailable(
                f"official card catalog returned {response.status_code}",
            )
        return response

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        missing_is_not_found: bool = False,
    ) -> Mapping[str, Any]:
        response = await self._get(
            url, params=params, missing_is_not_found=missing_is_not_found,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OfficialCardCatalogUnavailable(
                "official card catalog response is not valid JSON",
            ) from exc
        if not isinstance(payload, Mapping):
            raise OfficialCardCatalogUnavailable(
                "official card catalog response is not a JSON object",
            )
        declared = payload.get("schema_version")
        if isinstance(declared, int) and declared > CATALOG_SCHEMA_VERSION:
            raise OfficialCardCatalogUnavailable(
                f"official card catalog schema_version {declared} is newer "
                f"than this build supports ({CATALOG_SCHEMA_VERSION})",
            )
        return payload

    def _as_summary(self, entry: object) -> OfficialCardSummary | None:
        if not isinstance(entry, Mapping):
            return None
        card_id = _text(entry.get("id"))
        if not card_id:
            # One malformed row must not blank the shelf — skip it the way
            # the local pack catalogue skips an unreadable file.
            _LOGGER.warning("official card catalog: entry without an id — skipping")
            return None
        return OfficialCardSummary(
            id=card_id,
            title=_text(entry.get("title")),
            summary=_text(entry.get("summary")),
            tags=_text_tuple(entry.get("tags")),
            author=_text(entry.get("author")),
            localized=entry.get("localized") is True,
            image_url=self.absolutise(_text(entry.get("image_url"))) or None,
            image_count=_count(entry.get("image_count")),
        )

    def _as_images(self, raw: object) -> tuple[OfficialCardImage, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        images: list[OfficialCardImage] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            url = self.absolutise(_text(entry.get("url")))
            if not url:
                continue
            images.append(OfficialCardImage(
                index=_count(entry.get("index")),
                url=url,
                content_type=_text(entry.get("content_type")),
                width=_count(entry.get("width")),
                height=_count(entry.get("height")),
                variants=_text_tuple(entry.get("variants")),
            ))
        return tuple(images)

    def absolutise(self, path: str) -> str:
        """Promote a catalog-relative asset path to a browser-usable URL.

        Cloud answers with service-relative paths because it cannot know its
        own hostname (container edge, nginx edge, or whatever a self-hoster
        pointed their catalog env at). The base URL this client was
        configured with is the only place that knowledge exists.
        """
        if not path:
            return ""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            return path
        return f"{self._asset_origin}{path}"


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    fetched_at: float


class CachedOfficialCardCatalog(OfficialCardCatalogPort):
    """TTL cache with stale-while-error in front of the HTTP reader.

    The TTL is the ceiling on how long a freshly published card stays
    invisible to one deployment — minutes, deliberately, because thousands
    of self-hosted deployments polling a public endpoint is exactly the load
    the plan's three cache layers exist to flatten.

    Stale-while-error is the fail-soft rule in one line: an outage serves the
    previous answer, and only a deployment that has *never* successfully read
    the catalog sees ``None``. The card shelf empties out; nothing else in
    the app notices.

    There is deliberately no lock. Two players opening the gallery in the
    same second may both fetch once — a duplicated read of a public,
    cacheable document — where a lock would make every gallery open wait
    behind whichever request drew the slow refresh.
    """

    def __init__(
        self,
        *,
        client: OfficialCardCatalogClient,
        ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = max(1.0, ttl_seconds)
        self._now = time_source
        self._entries: dict[tuple[str, str], _CacheEntry] = {}

    async def list_cards(self, *, locale: str) -> list[OfficialCardSummary] | None:
        cached = await self._read(
            ("catalog", locale),
            lambda: self._client.fetch_catalog(locale=locale),
        )
        return None if cached is None else list(cached)

    async def get_card(
        self, card_id: str, *, locale: str, fresh: bool = False,
    ) -> OfficialCardDetail | None:
        return await self._read(
            (f"card:{card_id}", locale),
            lambda: self._client.fetch_card(card_id, locale=locale),
            fresh=fresh,
        )

    async def download_artifact(self, card_id: str) -> bytes | None:
        """Uncached on purpose.

        A ``.lumecard`` is megabytes and an install is a deliberate, rare
        act; holding every official card's zip in every worker process to
        save one download would trade a lot of memory for nothing. There is
        no stale copy to fall back on either — an install that cannot fetch
        the card must fail visibly rather than land something old.

        :class:`OfficialCardNotFound` propagates: "this card is gone" and
        "the download broke" are different things to tell a player.
        """
        try:
            return await self._client.fetch_artifact(card_id)
        except OfficialCardCatalogUnavailable:
            _LOGGER.warning(
                "official card artifact %s could not be downloaded", card_id,
                exc_info=True,
            )
            return None

    async def _read(
        self,
        key: tuple[str, str],
        fetch: Callable[[], Any],
        *,
        fresh: bool = False,
    ) -> Any:
        """Read through the cache, or deliberately around it.

        ``fresh`` inverts the stale-while-error rule for the one caller that
        cannot live with it. Browsing wants the last good answer during an
        outage; an install wants to stop, because the document it is skipping
        would be the only thing telling it the artifact it is about to
        download has moved on since those words were approved.
        """
        entry = self._entries.get(key)
        if (
            not fresh
            and entry is not None
            and (self._now() - entry.fetched_at) <= self._ttl
        ):
            return entry.value
        try:
            value = await fetch()
        except OfficialCardNotFound:
            # An answer, not a failure: forget what we held so a withdrawn
            # card stops being served the moment Cloud says it is gone.
            self._entries.pop(key, None)
            raise
        except OfficialCardCatalogUnavailable:
            if fresh:
                _LOGGER.warning(
                    "official card catalog unavailable on a fresh read of %s "
                    "— refusing rather than pairing cached text with a live "
                    "download", key, exc_info=True,
                )
                return None
            if entry is None:
                _LOGGER.warning(
                    "official card catalog unavailable and nothing cached for %s",
                    key, exc_info=True,
                )
                return None
            _LOGGER.warning(
                "official card catalog unavailable — serving stale %s", key,
                exc_info=True,
            )
            # Deliberately not re-stamped: stale content keeps aging so a
            # long outage still retries on every request rather than
            # freezing yesterday's catalog in place for good.
            return entry.value
        # A fresh read still refills the cache: it is the newest answer this
        # process has, and letting the browse path keep an older one because
        # the install path fetched it would be a cache that ages backwards.
        self._entries[key] = _CacheEntry(value=value, fetched_at=self._now())
        return value


def _asset_origin(base_url: str) -> str:
    """The prefix that catalog-relative asset paths hang off.

    ``https://app.yuralume.com/api/user/v1/public/official-cards`` →
    ``https://app.yuralume.com/api/user``. A configured URL that does not end
    in the known mount path (a self-hoster's own proxy layout) falls back to
    the bare origin, which is what a plain relative-path join would have
    done anyway.
    """
    if not base_url:
        return ""
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    prefix = (
        path[: -len(_PUBLIC_BASE_PATH)]
        if path.endswith(_PUBLIC_BASE_PATH)
        else ""
    )
    return urlunsplit((parsed.scheme, parsed.netloc, prefix, "", ""))


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str))


def _count(value: object) -> int:
    # bool is an int subclass; a boolean here means the shape changed.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


__all__ = [
    "ARTIFACT_CHUNK_BYTES",
    "CATALOG_SCHEMA_VERSION",
    "MAX_ARTIFACT_BYTES",
    "CachedOfficialCardCatalog",
    "OfficialCardCatalogClient",
]
