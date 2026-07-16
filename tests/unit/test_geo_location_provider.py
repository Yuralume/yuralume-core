"""Unit tests for IP-backed GeoLocationPort adapters."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from kokoro_link.infrastructure.geo.ip_api_provider import IpApiGeoLocationProvider
from kokoro_link.infrastructure.geo.null_provider import NullGeoLocationProvider


class _StubClient:
    def __init__(
        self,
        *,
        json_payload: dict[str, Any] | None = None,
        status_code: int = 200,
        error: Exception | None = None,
    ) -> None:
        self.json_payload = json_payload or {}
        self.status_code = status_code
        self.error = error
        self.calls = 0
        self.last_url: str | None = None
        self.last_params: dict[str, Any] | None = None

    async def get(
        self, url: str, *, params: dict[str, Any] | None = None, timeout=None,
    ) -> httpx.Response:
        self.calls += 1
        self.last_url = url
        self.last_params = params
        if self.error is not None:
            raise self.error
        return httpx.Response(
            status_code=self.status_code,
            json=self.json_payload,
            request=httpx.Request("GET", url, params=params),
        )


def test_ip_api_provider_parses_success_payload() -> None:
    client = _StubClient(json_payload={
        "status": "success",
        "countryCode": "us",
        "lat": 37.7749,
        "lon": -122.4194,
        "city": "San Francisco",
        "regionName": "California",
        "country": "United States",
        "timezone": "America/Los_Angeles",
    })
    provider = IpApiGeoLocationProvider(http_client=client)  # type: ignore[arg-type]

    location = asyncio.run(provider.locate("8.8.8.8"))

    assert location is not None
    assert location.country_code == "US"
    assert location.latitude == 37.7749
    assert location.longitude == -122.4194
    assert location.label == "San Francisco, California, US"
    assert location.timezone_id == "America/Los_Angeles"
    assert client.calls == 1
    assert client.last_url == "http://ip-api.com/json/8.8.8.8"
    assert client.last_params is None


def test_ip_api_provider_leaves_timezone_none_when_payload_omits_it() -> None:
    client = _StubClient(json_payload={
        "status": "success",
        "countryCode": "tw",
        "lat": 25.0,
        "lon": 121.5,
    })
    provider = IpApiGeoLocationProvider(http_client=client)  # type: ignore[arg-type]

    location = asyncio.run(provider.locate("8.8.8.8"))

    assert location is not None
    assert location.country_code == "TW"
    assert location.timezone_id is None


def test_ip_api_provider_skips_loopback_and_private_ips() -> None:
    client = _StubClient()
    provider = IpApiGeoLocationProvider(http_client=client)  # type: ignore[arg-type]

    assert asyncio.run(provider.locate("127.0.0.1")) is None
    assert asyncio.run(provider.locate("192.168.1.8")) is None
    assert asyncio.run(provider.locate("::1")) is None
    assert client.calls == 0


def test_ip_api_provider_failure_paths_return_none() -> None:
    timeout = _StubClient(error=httpx.TimeoutException("slow"))
    assert asyncio.run(
        IpApiGeoLocationProvider(http_client=timeout).locate("8.8.8.8")  # type: ignore[arg-type]
    ) is None

    bad_status = _StubClient(status_code=502)
    assert asyncio.run(
        IpApiGeoLocationProvider(http_client=bad_status).locate("8.8.8.8")  # type: ignore[arg-type]
    ) is None

    malformed = _StubClient(json_payload={"status": "success", "lat": "nope"})
    assert asyncio.run(
        IpApiGeoLocationProvider(http_client=malformed).locate("8.8.8.8")  # type: ignore[arg-type]
    ) is None


def test_null_geo_location_provider_always_returns_none() -> None:
    assert asyncio.run(NullGeoLocationProvider().locate("8.8.8.8")) is None
