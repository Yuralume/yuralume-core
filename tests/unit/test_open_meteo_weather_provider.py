from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.contracts.weather_context import WeatherLocation
from kokoro_link.infrastructure.weather.open_meteo_provider import (
    OpenMeteoWeatherProvider,
)


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, *, params: dict[str, Any], timeout) -> _Response:
        _ = url, timeout
        self.calls.append(dict(params))
        return _Response({
            "current": {"temperature_2m": 21.0, "weather_code": 0, "is_day": 1},
            "daily": {
                "temperature_2m_max": [25.0],
                "temperature_2m_min": [18.0],
                "precipitation_probability_max": [10],
            },
        })


@pytest.mark.asyncio
async def test_cache_is_scoped_per_location() -> None:
    client = _Client()
    provider = OpenMeteoWeatherProvider(
        latitude=25.0,
        longitude=121.5,
        location_label="Taipei",
        cache_ttl_seconds=900,
        http_client=client,  # type: ignore[arg-type]
    )
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    sf = WeatherLocation(37.7749, -122.4194, "San Francisco")
    nyc = WeatherLocation(40.7128, -74.0060, "New York")

    first = await provider.describe(now=now, location=sf)
    second = await provider.describe(now=now + timedelta(minutes=1), location=sf)
    third = await provider.describe(now=now + timedelta(minutes=2), location=nyc)

    assert "San Francisco" in first
    assert "San Francisco" in second
    assert "New York" in third
    assert len(client.calls) == 2
    assert client.calls[0]["latitude"] == 37.7749
    assert client.calls[1]["latitude"] == 40.7128


@pytest.mark.asyncio
async def test_none_location_uses_deployment_fallback() -> None:
    client = _Client()
    provider = OpenMeteoWeatherProvider(
        latitude=25.0,
        longitude=121.5,
        location_label="Taipei",
        cache_ttl_seconds=900,
        http_client=client,  # type: ignore[arg-type]
    )

    block = await provider.describe(now=datetime(2026, 6, 3, tzinfo=timezone.utc))

    assert "Taipei" in block
    assert client.calls[0]["latitude"] == 25.0


@pytest.mark.asyncio
async def test_none_location_without_deployment_fallback_returns_empty() -> None:
    provider = OpenMeteoWeatherProvider(
        latitude=None,
        longitude=None,
        location_label="",
    )

    assert await provider.describe() == ""


def test_build_weather_provider_localizes_empty_deployment_label() -> None:
    # An unset deployment label must follow the deploy-time content language,
    # not the provider's hardcoded Chinese last resort (issue #5).
    from kokoro_link.bootstrap.container import _build_weather_provider
    from kokoro_link.bootstrap.settings import AppSettings, WeatherSettings

    provider = _build_weather_provider(
        settings=AppSettings(
            weather=WeatherSettings(latitude=1.0, longitude=2.0, location_label=""),
            default_primary_language="en-US",
        ),
    )

    assert provider._location_label == "Current location"


def test_build_weather_provider_keeps_explicit_deployment_label() -> None:
    from kokoro_link.bootstrap.container import _build_weather_provider
    from kokoro_link.bootstrap.settings import AppSettings, WeatherSettings

    provider = _build_weather_provider(
        settings=AppSettings(
            weather=WeatherSettings(
                latitude=1.0, longitude=2.0, location_label="Taipei",
            ),
            default_primary_language="en-US",
        ),
    )

    assert provider._location_label == "Taipei"
