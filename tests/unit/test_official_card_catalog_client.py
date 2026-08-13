"""The Cloud catalog reader and the cache that decides what to do when it
fails.

Driven by ``httpx.MockTransport`` so the wire shape is pinned against the
documents Cloud actually serves (relative asset paths, ``schema_version``,
``localized``), and so no test ever touches the network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kokoro_link.contracts.official_card_catalog import (
    OfficialCardCatalogUnavailable,
    OfficialCardNotFound,
)
from kokoro_link.infrastructure.cloud import official_card_catalog_client
from kokoro_link.infrastructure.cloud.official_card_catalog_client import (
    ARTIFACT_CHUNK_BYTES,
    MAX_ARTIFACT_BYTES,
    CachedOfficialCardCatalog,
    OfficialCardCatalogClient,
)

_BASE = "https://app.yuralume.com/api/user/v1/public/official-cards"

_CATALOG_BODY: dict[str, Any] = {
    "schema_version": 1,
    "locale": "ja",
    "cards": [
        {
            "id": "mio-cafe",
            "title": "ミオ",
            "summary": "カフェで働く大学生。",
            "tags": ["現代"],
            "author": "Yuralume",
            "localized": True,
            "image_url": "/v1/public/official-cards/mio-cafe/images/0",
            "image_count": 2,
            "updated_at": "2026-08-01T00:00:00Z",
            "published_at": "2026-08-01T00:00:00Z",
        },
    ],
}

_DETAIL_BODY: dict[str, Any] = {
    "schema_version": 1,
    "locale": "ja",
    "localized": True,
    "available_locales": ["zh-Hant", "en", "ja"],
    "card": {
        "id": "mio-cafe",
        "title": "ミオ",
        "description": "公式カード",
        "tags": ["現代"],
        "author": "Yuralume",
        "note": "アニメ調の profile 推奨",
        "artifact_url": "/v1/public/official-cards/mio-cafe/artifact",
        "images": [
            {
                "index": 0,
                "url": "/v1/public/official-cards/mio-cafe/images/0",
                "content_type": "image/png",
                "width": 1024,
                "height": 1536,
                "variants": ["w320", "w768"],
            },
        ],
        "profile": {
            "name": "ミオ",
            "summary": "カフェで働く大学生。",
            "personality": ["明るい"],
            "companions": [{"name": "ハル", "role": "同僚"}],
        },
        "updated_at": "2026-08-01T00:00:00Z",
        "published_at": "2026-08-01T00:00:00Z",
    },
}


def _json_response(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _client(handler) -> OfficialCardCatalogClient:
    return OfficialCardCatalogClient(
        base_url=_BASE, transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_catalog_rows_carry_absolute_image_urls() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(_CATALOG_BODY)

    cards = await _client(handler).fetch_catalog(locale="ja")

    assert [card.id for card in cards] == ["mio-cafe"]
    # The document says `/v1/public/...`; the deployed edge also carries an
    # `/api/user` prefix, and only this side knows that. Joining to the bare
    # origin would 404 every official portrait.
    assert cards[0].image_url == (
        "https://app.yuralume.com/api/user"
        "/v1/public/official-cards/mio-cafe/images/0"
    )
    assert cards[0].localized is True
    assert cards[0].image_count == 2
    assert seen[0].url.params["locale"] == "ja"


@pytest.mark.asyncio
async def test_the_request_carries_nothing_that_identifies_the_deployment() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(_CATALOG_BODY)

    await _client(handler).fetch_catalog(locale="en")

    sent = {name.lower() for name in seen[0].headers}
    # A self-hoster reading the public catalog must not thereby announce
    # that they exist. Anything beyond a plain GET's own plumbing is an
    # identifier nobody agreed to send.
    assert not sent & {
        "authorization",
        "cookie",
        "x-internal-token",
        "x-deployment-id",
        "x-yuralume-deployment",
    }
    assert seen[0].method == "GET"


@pytest.mark.asyncio
async def test_detail_passes_the_profile_map_through_untouched() -> None:
    detail = await _client(lambda _r: _json_response(_DETAIL_BODY)).fetch_card(
        "mio-cafe", locale="ja",
    )

    assert detail.localized is True
    assert detail.available_locales == ("zh-Hant", "en", "ja")
    # Not projected into fields: the key set is defined by the extractor on
    # the Cloud side and by Core's own card translator, and a third
    # definition here would drop the next field either of them adds.
    assert detail.profile["personality"] == ["明るい"]
    assert detail.profile["companions"] == [{"name": "ハル", "role": "同僚"}]
    assert detail.images[0].url.endswith(
        "/api/user/v1/public/official-cards/mio-cafe/images/0",
    )
    assert detail.images[0].variants == ("w320", "w768")


@pytest.mark.asyncio
async def test_a_stale_translation_crosses_the_wire_as_a_fact() -> None:
    """``stale`` rides beside ``localized`` and must reach the DTO.

    A translation approved against an older artifact is still shown while
    browsing (the catalog decides that), but the install path has to know —
    merging it into a re-uploaded card pastes yesterday's sentences onto
    today's fields by list position.
    """
    detail = await _client(
        lambda _r: _json_response(dict(_DETAIL_BODY, stale=True)),
    ).fetch_card("mio-cafe", locale="ja")

    assert detail.stale is True


@pytest.mark.asyncio
async def test_a_document_without_the_stale_field_reads_as_fresh() -> None:
    """Backward compatible: a Cloud that predates the field is not stale."""
    detail = await _client(lambda _r: _json_response(_DETAIL_BODY)).fetch_card(
        "mio-cafe", locale="ja",
    )

    assert detail.stale is False


@pytest.mark.asyncio
async def test_a_server_error_is_unavailable_not_an_empty_catalog() -> None:
    with pytest.raises(OfficialCardCatalogUnavailable):
        await _client(lambda _r: httpx.Response(500)).fetch_catalog(locale="en")


@pytest.mark.asyncio
async def test_a_timeout_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(OfficialCardCatalogUnavailable):
        await _client(handler).fetch_catalog(locale="en")


@pytest.mark.asyncio
async def test_a_missing_card_is_not_found_not_unavailable() -> None:
    """404 is an answer, and the difference decides the cache's behaviour."""
    client = _client(lambda _r: httpx.Response(404))

    with pytest.raises(OfficialCardNotFound):
        await client.fetch_card("withdrawn", locale="en")
    with pytest.raises(OfficialCardNotFound):
        await client.fetch_artifact("withdrawn")


@pytest.mark.asyncio
async def test_a_newer_schema_version_is_refused_rather_than_half_read() -> None:
    body = dict(_CATALOG_BODY, schema_version=2)

    with pytest.raises(OfficialCardCatalogUnavailable):
        await _client(lambda _r: _json_response(body)).fetch_catalog(locale="en")


@pytest.mark.asyncio
async def test_an_artifact_download_is_bounded() -> None:
    """A card is single-digit megabytes; anything else is not a card.

    The importer's zip guard would reject it too, but only after the whole
    thing had been pulled into memory.
    """
    oversized = b"\x00" * (MAX_ARTIFACT_BYTES + 1)

    with pytest.raises(OfficialCardCatalogUnavailable):
        await _client(lambda _r: httpx.Response(200, content=oversized)).fetch_artifact(
            "mio-cafe",
        )


class _EndlessStream(httpx.AsyncByteStream):
    """A body that declares no length and keeps coming.

    The shape the cap exists for: a redirect to something enormous, or a
    compromised catalog. ``sent`` is how many bytes the client actually
    pulled off the wire, which is the only way to tell "refused after
    reading all of it" from "refused and hung up".
    """

    def __init__(self, *, chunk_size: int, chunks: int) -> None:
        self._chunk = b"\x00" * chunk_size
        self._chunks = chunks
        self.sent = 0

    async def __aiter__(self):
        for _ in range(self._chunks):
            self.sent += len(self._chunk)
            yield self._chunk


@pytest.mark.asyncio
async def test_an_oversized_artifact_is_cut_off_mid_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap must bound memory, not merely describe it.

    Checking ``len(response.content)`` would mean httpx had already
    materialised the whole body — the exact allocation the limit is
    supposed to prevent on a path any logged-in player can trigger.
    """
    monkeypatch.setattr(official_card_catalog_client, "MAX_ARTIFACT_BYTES", 4096)
    stream = _EndlessStream(chunk_size=1024, chunks=4096)

    def handler(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, stream=stream)
        # No length to trust: the guard cannot be a header check.
        assert "content-length" not in response.headers
        return response

    with pytest.raises(OfficialCardCatalogUnavailable):
        await _client(handler).fetch_artifact("mio-cafe")

    # One chunk of overshoot is what it takes to know the cap was crossed;
    # 4 MB of overshoot would mean nothing was aborted. The chunk is the size
    # this client asks for rather than whatever the transport felt like
    # handing over — that is the point of pinning it, because for a gzipped
    # body the transport's answer is a number the sender chooses.
    assert stream.sent <= 4096 + ARTIFACT_CHUNK_BYTES


@pytest.mark.asyncio
async def test_an_artifact_download_refuses_a_compressed_body() -> None:
    """The size counter cannot count what has not been inflated yet.

    ``aiter_bytes`` decodes a whole transport read before chunking it, so an
    accepted ``gzip`` encoding puts the sender in charge of how many bytes
    are resident before the first check runs — a few kilobytes of crafted
    stream is tens of megabytes in this process. A ``.lumecard`` is already a
    zip, so declining the encoding costs nothing and removes the multiplier.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"PK\x03\x04")

    await _client(handler).fetch_artifact("mio-cafe")

    assert seen[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_an_artifact_within_the_cap_still_arrives_whole() -> None:
    """Streaming must not truncate the ordinary case."""
    blob = b"PK\x03\x04" + b"\x11" * 5000

    downloaded = await _client(
        lambda _r: httpx.Response(200, content=blob),
    ).fetch_artifact("mio-cafe")

    assert downloaded == blob


@pytest.mark.asyncio
async def test_a_mistyped_catalog_url_is_unavailable_not_a_crash() -> None:
    """A self-hoster's typo must cost them the official shelf, nothing else.

    ``httpx.InvalidURL`` is not an ``HTTPError``, so without an explicit
    catch it would raise straight through the cache and 500 the gallery —
    taking the local packs down with it.
    """
    client = OfficialCardCatalogClient(base_url="not a url at all")

    with pytest.raises(OfficialCardCatalogUnavailable):
        await client.fetch_catalog(locale="en")


@pytest.mark.asyncio
async def test_an_unconfigured_url_never_leaves_the_process() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a disabled catalog must not make a request")

    client = OfficialCardCatalogClient(
        base_url="", transport=httpx.MockTransport(handler),
    )

    assert client.configured is False
    with pytest.raises(OfficialCardCatalogUnavailable):
        await client.fetch_catalog(locale="en")


@pytest.mark.asyncio
async def test_a_self_hosted_url_without_the_known_mount_falls_back_to_origin() -> None:
    client = OfficialCardCatalogClient(base_url="http://cards.internal:8080/cards")

    assert client.absolutise("/v1/public/official-cards/x/images/0") == (
        "http://cards.internal:8080/v1/public/official-cards/x/images/0"
    )
    # An absolute URL from a future document shape is left alone.
    assert client.absolutise("https://cdn.example/x.png") == "https://cdn.example/x.png"


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #


class _FakeReader:
    """Stands in for the HTTP client: counts calls, fails on demand."""

    def __init__(self) -> None:
        self.calls = 0
        self.failure: Exception | None = None
        self.title = "first"

    async def fetch_catalog(self, *, locale: str):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        from kokoro_link.contracts.official_card_catalog import OfficialCardSummary
        return [OfficialCardSummary(id="mio-cafe", title=self.title)]

    async def fetch_card(self, card_id: str, *, locale: str):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        from kokoro_link.contracts.official_card_catalog import OfficialCardDetail
        return OfficialCardDetail(id=card_id, title=self.title)

    async def fetch_artifact(self, card_id: str) -> bytes:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return b"PK\x03\x04"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _cached(reader: _FakeReader, clock: _Clock) -> CachedOfficialCardCatalog:
    return CachedOfficialCardCatalog(
        client=reader, ttl_seconds=600.0, time_source=clock,
    )


@pytest.mark.asyncio
async def test_a_warm_cache_answers_without_asking_cloud_again() -> None:
    reader, clock = _FakeReader(), _Clock()
    cache = _cached(reader, clock)

    await cache.list_cards(locale="ja")
    clock.now = 599.0
    await cache.list_cards(locale="ja")

    assert reader.calls == 1


@pytest.mark.asyncio
async def test_a_different_locale_is_a_different_entry() -> None:
    reader, clock = _FakeReader(), _Clock()
    cache = _cached(reader, clock)

    await cache.list_cards(locale="ja")
    await cache.list_cards(locale="en")

    assert reader.calls == 2


@pytest.mark.asyncio
async def test_an_expired_entry_is_refetched() -> None:
    reader, clock = _FakeReader(), _Clock()
    cache = _cached(reader, clock)

    await cache.list_cards(locale="ja")
    clock.now = 601.0
    reader.title = "second"
    cards = await cache.list_cards(locale="ja")

    assert reader.calls == 2
    assert cards is not None and cards[0].title == "second"


@pytest.mark.asyncio
async def test_an_outage_serves_the_last_good_answer() -> None:
    reader, clock = _FakeReader(), _Clock()
    cache = _cached(reader, clock)

    await cache.list_cards(locale="ja")
    clock.now = 601.0
    reader.failure = OfficialCardCatalogUnavailable("cloud is down")
    cards = await cache.list_cards(locale="ja")

    assert cards is not None and cards[0].title == "first"


@pytest.mark.asyncio
async def test_an_outage_with_nothing_cached_is_none_not_an_empty_list() -> None:
    """``None`` and ``[]`` are different claims.

    An empty list says the catalog is genuinely empty; the shelf would look
    the same as a withdrawn-everything state and nobody could tell the
    player anything true.
    """
    reader, clock = _FakeReader(), _Clock()
    reader.failure = OfficialCardCatalogUnavailable("cloud is down")

    assert await _cached(reader, clock).list_cards(locale="ja") is None


@pytest.mark.asyncio
async def test_a_withdrawn_card_stops_being_served_immediately() -> None:
    reader, clock = _FakeReader(), _Clock()
    cache = _cached(reader, clock)

    await cache.get_card("mio-cafe", locale="ja")
    clock.now = 601.0
    reader.failure = OfficialCardNotFound("withdrawn")

    with pytest.raises(OfficialCardNotFound):
        await cache.get_card("mio-cafe", locale="ja")
    # Evicted, not held as "stale": a withdrawal is an answer, and folding
    # it into the outage path would pin the card in memory for the life of
    # the process.
    reader.failure = OfficialCardCatalogUnavailable("cloud is down")
    assert await cache.get_card("mio-cafe", locale="ja") is None


@pytest.mark.asyncio
async def test_a_fresh_read_goes_past_a_warm_entry() -> None:
    """The install path's read must see what Cloud says right now.

    The artifact beside it is downloaded live every time, so a cached detail
    is a document describing a possibly-older version of the very bytes about
    to be imported — including its ``stale`` flag, which is the only thing
    telling the importer not to merge them.
    """
    reader, clock = _FakeReader(), _Clock()
    cache = _cached(reader, clock)

    await cache.get_card("mio-cafe", locale="ja")
    reader.title = "second"
    fresh = await cache.get_card("mio-cafe", locale="ja", fresh=True)

    assert reader.calls == 2
    assert fresh is not None and fresh.title == "second"
    # And it refills the entry rather than leaving an older one behind: a
    # cache that ages backwards would be worse than either behaviour.
    cached = await cache.get_card("mio-cafe", locale="ja")
    assert reader.calls == 2
    assert cached is not None and cached.title == "second"


@pytest.mark.asyncio
async def test_a_fresh_read_that_fails_is_none_rather_than_last_known_good() -> None:
    """Stale-while-error is inverted for the one caller it would betray.

    Browsing during an outage is better served by a ten-minute-old
    description than by an empty shelf. An install is not: falling back here
    means pairing cached words with a live download, which is precisely the
    mismatch ``fresh`` was introduced to close.
    """
    reader, clock = _FakeReader(), _Clock()
    cache = _cached(reader, clock)

    await cache.get_card("mio-cafe", locale="ja")
    reader.failure = OfficialCardCatalogUnavailable("cloud is down")

    assert await cache.get_card("mio-cafe", locale="ja", fresh=True) is None
    # The entry is not evicted, so browsing keeps its last good answer.
    assert await cache.get_card("mio-cafe", locale="ja") is not None


@pytest.mark.asyncio
async def test_a_failed_artifact_download_is_none_and_never_cached() -> None:
    reader, clock = _FakeReader(), _Clock()
    cache = _cached(reader, clock)

    assert await cache.download_artifact("mio-cafe") == b"PK\x03\x04"
    assert await cache.download_artifact("mio-cafe") == b"PK\x03\x04"
    assert reader.calls == 2

    reader.failure = OfficialCardCatalogUnavailable("cloud is down")
    assert await cache.download_artifact("mio-cafe") is None


# --------------------------------------------------------------------------- #
# cloud-exclusive cards (EC4)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_catalog_row_carries_its_distribution() -> None:
    body = {
        **_CATALOG_BODY,
        "cards": [{**_CATALOG_BODY["cards"][0], "distribution": "cloud_exclusive"}],
    }

    cards = await _client(lambda _r: _json_response(body)).fetch_catalog(
        locale="ja",
    )

    assert cards[0].distribution == "cloud_exclusive"


@pytest.mark.asyncio
async def test_a_row_without_the_field_is_read_as_public() -> None:
    # A catalog that predates exclusivity has no exclusive cards to
    # describe; defaulting the other way would grey out every install.
    cards = await _client(lambda _r: _json_response(_CATALOG_BODY)).fetch_catalog(
        locale="ja",
    )

    assert cards[0].distribution == "public"


@pytest.mark.asyncio
async def test_a_withheld_artifact_url_is_how_a_detail_says_cloud_exclusive(
) -> None:
    body = {
        **_DETAIL_BODY,
        "card": {**_DETAIL_BODY["card"], "artifact_url": None, "profile": {}},
    }

    detail = await _client(lambda _r: _json_response(body)).fetch_card(
        "mio-cafe", locale="ja",
    )

    assert detail.artifact_published is False
    assert detail.cloud_exclusive is True


@pytest.mark.asyncio
async def test_an_ordinary_detail_is_not_exclusive() -> None:
    detail = await _client(lambda _r: _json_response(_DETAIL_BODY)).fetch_card(
        "mio-cafe", locale="ja",
    )

    assert detail.cloud_exclusive is False


@pytest.mark.asyncio
async def test_a_detail_with_no_artifact_key_at_all_is_not_exclusive() -> None:
    # A missing key is a Cloud from before exclusivity existed, where every
    # published card had an artifact — different from a key set to null.
    card = {key: value for key, value in _DETAIL_BODY["card"].items()
            if key != "artifact_url"}

    detail = await _client(
        lambda _r: _json_response({**_DETAIL_BODY, "card": card}),
    ).fetch_card("mio-cafe", locale="ja")

    assert detail.cloud_exclusive is False


@pytest.mark.asyncio
async def test_stage_image_bytes_download_from_the_catalogs_own_origin() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"\x89PNG")

    data = await _client(handler).fetch_image(
        "https://app.yuralume.com/api/user"
        "/v1/public/official-cards/mio-cafe/images/0",
    )

    assert data == b"\x89PNG"
    assert seen == [
        "https://app.yuralume.com/api/user"
        "/v1/public/official-cards/mio-cafe/images/0",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    # A different host that merely starts with the same characters.
    "https://app.yuralume.com.evil.test/api/user/v1/public/x/images/0",
    # A different host entirely.
    "https://evil.test/v1/public/official-cards/mio-cafe/images/0",
    # The right host, outside the mount prefix.
    "https://app.yuralume.com/internal/secrets",
    # Not http at all.
    "file:///etc/passwd",
])
async def test_an_image_url_outside_the_asset_origin_is_never_fetched(
    url: str,
) -> None:
    # The URL arrives inside a document, and a document is data: without
    # this a catalog answer could aim an outbound request from inside the
    # deployment at anywhere at all.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"must not fetch {request.url}")

    with pytest.raises(OfficialCardCatalogUnavailable):
        await _client(handler).fetch_image(url)


@pytest.mark.asyncio
async def test_an_oversized_image_is_hung_up_on_rather_than_held() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * (official_card_catalog_client.MAX_IMAGE_BYTES + 1),
        )

    with pytest.raises(OfficialCardCatalogUnavailable):
        await _client(handler).fetch_image(
            "https://app.yuralume.com/api/user"
            "/v1/public/official-cards/mio-cafe/images/0",
        )
