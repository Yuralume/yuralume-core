"""Unit tests for :class:`OpenMeteoWeatherProvider`.

Uses a hand-rolled async client stub injected via the constructor's
``http_client`` slot — same pattern existing tests use for HTTP-backed
adapters (avoids pulling respx as a new dep just for one adapter).

Covers:

* Happy path: parsed payload renders WeatherFacts → prompt block.
* TTL cache: a second describe within the window doesn't re-hit HTTP.
* Network failure: ``""`` returned, no exception leaked.
* Malformed payload: ``""`` returned, no exception leaked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from kokoro_link.infrastructure.weather.open_meteo_provider import (
    NullWeatherProvider,
    OpenMeteoWeatherProvider,
)


class _StubClient:
    """Minimal stand-in for an ``httpx.AsyncClient`` GET path.

    Counts calls so cache assertions stay precise; raises ``error`` when
    configured so failure-path tests don't need a real network down."""

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

    async def get(self, _url: str, *, params: dict, timeout=None) -> httpx.Response:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return httpx.Response(
            status_code=self.status_code,
            json=self.json_payload,
            request=httpx.Request("GET", _url, params=params),
        )


_GOOD_PAYLOAD = {
    "current": {
        "time": "2026-05-19T10:00",
        "temperature_2m": 23.4,
        "weather_code": 3,
        "is_day": 1,
    },
    "daily": {
        "time": ["2026-05-19"],
        "temperature_2m_max": [26.0],
        "temperature_2m_min": [21.0],
        "precipitation_probability_max": [70],
    },
}


def _make_provider(client: _StubClient, *, ttl: int = 900) -> OpenMeteoWeatherProvider:
    return OpenMeteoWeatherProvider(
        latitude=25.04,
        longitude=121.56,
        location_label="台北",
        cache_ttl_seconds=ttl,
        http_client=client,  # type: ignore[arg-type]
    )


def test_happy_path_renders_full_block() -> None:
    client = _StubClient(json_payload=_GOOD_PAYLOAD)
    provider = _make_provider(client)
    block = asyncio.run(provider.describe())
    assert "台北" in block
    assert "陰天" in block  # WMO 3
    assert "23.4°C" in block
    assert "高溫 26.0°C" in block
    assert "低溫 21.0°C" in block
    assert "70%" in block
    assert "提醒：今日有相當機率會下雨" in block  # 70 ≥ 60 threshold
    assert client.calls == 1


def test_cache_hits_avoid_second_http_call() -> None:
    client = _StubClient(json_payload=_GOOD_PAYLOAD)
    provider = _make_provider(client, ttl=900)
    now = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    first = asyncio.run(provider.describe(now=now))
    # 5 minutes later, well within the 15-minute TTL → no HTTP call.
    second = asyncio.run(provider.describe(now=now + timedelta(minutes=5)))
    assert first == second
    assert client.calls == 1


def test_cache_expires_after_ttl() -> None:
    client = _StubClient(json_payload=_GOOD_PAYLOAD)
    provider = _make_provider(client, ttl=60)  # 60s TTL
    now = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    asyncio.run(provider.describe(now=now))
    # Two minutes later → TTL expired, second HTTP call fires.
    asyncio.run(provider.describe(now=now + timedelta(minutes=2)))
    assert client.calls == 2


def test_http_failure_returns_empty_string() -> None:
    client = _StubClient(error=httpx.ConnectError("network down"))
    provider = _make_provider(client)
    assert asyncio.run(provider.describe()) == ""


def test_malformed_payload_returns_empty_string() -> None:
    # Open-Meteo returning a list at root (impossible in practice, but
    # the adapter must not propagate a TypeError to the chat path).
    client = _StubClient(json_payload={"current": "not a dict"})
    provider = _make_provider(client)
    # ``current`` not a dict means all current fields collapse to None;
    # has_any_signal is False, so the renderer emits an empty string.
    assert asyncio.run(provider.describe()) == ""


def test_null_provider_always_empty() -> None:
    assert asyncio.run(NullWeatherProvider().describe()) == ""


def test_missing_daily_arrays_skip_high_low() -> None:
    payload = {
        "current": {"temperature_2m": 20.0, "weather_code": 0, "is_day": 1},
        "daily": {},
    }
    client = _StubClient(json_payload=payload)
    provider = _make_provider(client)
    block = asyncio.run(provider.describe())
    assert "20.0°C" in block
    assert "高溫" not in block  # daily arrays empty → no high/low lines
