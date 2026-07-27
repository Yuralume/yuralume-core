"""G2 — Open-Meteo geocoding adapter: parsing, language, fail-soft."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.infrastructure.geo.open_meteo_geocoding_client import (
    OpenMeteoGeocodingClient,
    _language_parameter,
)

_TAIPEI = {
    "name": "臺北市",
    "latitude": 25.04776,
    "longitude": 121.53185,
    "country_code": "TW",
    "country": "臺灣",
    "admin1": "臺北市",
    "timezone": "Asia/Taipei",
}


def _client(handler) -> OpenMeteoGeocodingClient:  # noqa: ANN001 - httpx handler
    transport = httpx.MockTransport(handler)
    return OpenMeteoGeocodingClient(
        http_client=httpx.AsyncClient(transport=transport),
    )


def _json_handler(payload: Any, captured: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(200, json=payload)

    return handler


@pytest.mark.asyncio
async def test_parses_results_into_places() -> None:
    client = _client(_json_handler({"results": [_TAIPEI]}))

    places = await client.search("台北")

    assert len(places) == 1
    place = places[0]
    assert place.country_code == "TW"
    assert place.timezone_id == "Asia/Taipei"
    assert place.latitude == pytest.approx(25.04776)
    # Label disambiguates same-named towns without repeating a duplicate part.
    assert place.label == "臺北市, 臺灣"


@pytest.mark.asyncio
async def test_sends_the_localised_language_and_count() -> None:
    captured: list[httpx.Request] = []
    client = _client(_json_handler({"results": []}, captured))

    await client.search("台北", language="zh-TW", limit=3)

    assert captured[0].url.params["language"] == "zh"
    assert captured[0].url.params["count"] == "3"
    assert captured[0].url.params["name"] == "台北"


@pytest.mark.asyncio
async def test_a_too_short_query_never_leaves_the_process() -> None:
    captured: list[httpx.Request] = []
    client = _client(_json_handler({"results": [_TAIPEI]}, captured))

    assert await client.search(" ") == []
    assert await client.search("a") == []
    assert captured == []


@pytest.mark.asyncio
async def test_no_matches_degrades_to_an_empty_list() -> None:
    """Open-Meteo omits ``results`` entirely when nothing matched."""
    client = _client(_json_handler({"generationtime_ms": 0.3}))

    assert await client.search("zzzzzzzz") == []


@pytest.mark.asyncio
async def test_upstream_failure_is_fail_soft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = _client(handler)

    assert await client.search("台北") == []


@pytest.mark.asyncio
async def test_a_timeout_is_fail_soft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _client(handler)

    assert await client.search("台北") == []


@pytest.mark.asyncio
async def test_malformed_rows_are_skipped_not_fatal() -> None:
    client = _client(
        _json_handler({"results": [{"name": "broken"}, _TAIPEI, "nonsense"]}),
    )

    places = await client.search("台北")

    assert [place.country_code for place in places] == ["TW"]


@pytest.mark.asyncio
async def test_the_result_count_is_clamped() -> None:
    captured: list[httpx.Request] = []
    client = _client(_json_handler({"results": []}, captured))

    await client.search("台北", limit=999)

    assert captured[0].url.params["count"] == "10"


def test_language_parameter_projection() -> None:
    assert _language_parameter("zh-TW") == "zh"
    assert _language_parameter("ja") == "ja"
    assert _language_parameter("en-US") == "en"
    # Unusable values are dropped rather than forwarded and rejected upstream.
    assert _language_parameter(None) is None
    assert _language_parameter("") is None
    assert _language_parameter("123") is None
