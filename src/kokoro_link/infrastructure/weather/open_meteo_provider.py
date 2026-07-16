"""Open-Meteo backed :class:`WeatherContextPort` implementation.

Open-Meteo（https://open-meteo.com）提供免 API key 的全球天氣 forecast，
非商業使用免費；對 Yuralume「事實層注入」的需求剛好夠用。所有解碼
都收斂在這個 adapter，prompt builder 看到的永遠是 :class:`WeatherFacts`。

設計重點：

* **TTL cache** —— 一個位置的天氣 15 分鐘內視為新鮮。減少 chat path 每
  輪都打 HTTP 的成本；天氣資料天然是慢變數，這個粒度足以反映實況。
* **失敗 graceful degrade** —— 上游錯誤、timeout、JSON 結構不符一律
  回 ``""``。calendar adapter 走同樣策略：缺一個事實層比讓整個 chat
  炸掉好。
* **沒有 retry** —— 一次失敗就 fallback；下次 tick / 下輪 chat 自然會
  再試。重試會把整個 prompt 路徑變慢，不值得。

env / settings 給 lat / lon / region label，預設用空字串/None 讓
container 在沒設定時 fallback 到 :class:`NullWeatherProvider`。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from kokoro_link.contracts.weather_context import WeatherContextPort, WeatherLocation
from kokoro_link.infrastructure.weather.facts import WeatherFacts


_LOGGER = logging.getLogger(__name__)


_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
"""短 timeout —— 天氣不是 chat 主路徑，超過 10 秒拿不到資料就放棄。
prompt 寧可少一段事實層，也不能因為天氣 API 慢拖累整輪聊天。"""

_DEFAULT_CACHE_TTL_SECONDS = 15 * 60
"""15 分鐘 cache —— 天氣不是即時變數，這個粒度對 LLM 行為差異足夠。
若 operator 想要更新鮮可以調短，但別調短到 60 秒以下，否則 chat 高峰
期會頻繁打 API。"""

_OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoWeatherProvider(WeatherContextPort):
    """Open-Meteo forecast adapter。"""

    def __init__(
        self,
        *,
        latitude: float | None,
        longitude: float | None,
        location_label: str,
        timezone_id: str = "auto",
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # Default coordinates come from deployment env and are now a
        # fallback only. Per-operator coordinates can be passed to
        # ``describe(location=...)`` without requiring a global env
        # location to exist.
        self._latitude = float(latitude) if latitude is not None else None
        self._longitude = float(longitude) if longitude is not None else None
        self._location_label = (location_label or "").strip() or "目前位置"
        self._timezone_id = (timezone_id or "auto").strip() or "auto"
        self._cache_ttl = max(60, int(cache_ttl_seconds))
        # 注入 client 供測試替換；正式環境每次自己建短生命週期 client。
        self._client = http_client
        self._cache_payloads: dict[
            tuple[float, float, str], tuple[dict[str, Any], float]
        ] = {}

    async def describe(
        self,
        *,
        now: datetime | None = None,
        location: WeatherLocation | None = None,
    ) -> str:
        resolved = self._resolve_location(location)
        if resolved is None:
            return ""
        payload = await self._fetch_or_cached(resolved, now=now)
        if payload is None:
            return ""
        facts = _parse_open_meteo(payload, location_label=resolved.label)
        if facts is None:
            return ""
        return facts.to_prompt_block()

    async def _fetch_or_cached(
        self,
        location: WeatherLocation,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        moment = now or datetime.now(timezone.utc)
        cache_key = _cache_key(location)
        cached = self._cache_payloads.get(cache_key)
        if cached is not None:
            payload, cached_at = cached
            elapsed = moment.timestamp() - cached_at
            if elapsed < self._cache_ttl:
                return payload
        try:
            payload = await self._http_get(location)
        except (httpx.HTTPError, ValueError) as exc:
            # graceful degrade —— 記 warn 一次，下次 chat 自然再試。
            _LOGGER.warning(
                "Open-Meteo weather fetch failed (%s); falling back to "
                "empty weather block",
                exc,
            )
            return None
        self._cache_payloads[cache_key] = (payload, moment.timestamp())
        return payload

    async def _http_get(self, location: WeatherLocation) -> dict[str, Any]:
        params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timezone": location.timezone_id,
            "current": "temperature_2m,weather_code,is_day",
            "daily": ",".join((
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
            )),
            # forecast_days=1 因為我們只想知道「今天高低溫＋目前」；多
            # 抓幾天浪費 quota 也讓 cache invalidation 更難對齊。
            "forecast_days": 1,
        }
        if self._client is not None:
            response = await self._client.get(
                _OPEN_METEO_FORECAST_URL, params=params, timeout=_REQUEST_TIMEOUT,
            )
        else:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(_OPEN_METEO_FORECAST_URL, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Open-Meteo response is not a JSON object")
        return data

    def _resolve_location(
        self,
        location: WeatherLocation | None,
    ) -> WeatherLocation | None:
        if location is not None:
            return location
        if self._latitude is None or self._longitude is None:
            return None
        return WeatherLocation(
            latitude=self._latitude,
            longitude=self._longitude,
            label=self._location_label,
            timezone_id=self._timezone_id,
        )


def _parse_open_meteo(
    payload: dict[str, Any], *, location_label: str,
) -> WeatherFacts:
    """Decode an Open-Meteo forecast payload into :class:`WeatherFacts`.

    Defensive throughout: any missing / wrong-typed field becomes
    ``None`` so the renderer suppresses that line rather than the
    whole block. This shape lets us hand-roll mock payloads in tests
    with only the fields we care about for that assertion.
    """
    # Defensive: Open-Meteo always returns dicts on a successful 200,
    # but a partial / replayed / malformed body shouldn't blow up the
    # chat path with AttributeError. Coerce non-dicts to empty dict so
    # every downstream ``.get()`` sees something it can handle.
    current = payload.get("current")
    if not isinstance(current, dict):
        current = {}
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        daily = {}
    temp_max_list = daily.get("temperature_2m_max")
    temp_min_list = daily.get("temperature_2m_min")
    prcp_list = daily.get("precipitation_probability_max")
    return WeatherFacts(
        location_label=location_label,
        condition_code=_coerce_int(current.get("weather_code")),
        temperature_c=_coerce_float(current.get("temperature_2m")),
        high_c=_first_float(temp_max_list),
        low_c=_first_float(temp_min_list),
        precipitation_probability=_first_int(prcp_list),
        is_day=_coerce_is_day(current.get("is_day")),
    )


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_is_day(value: Any) -> bool | None:
    """Open-Meteo 回 0 / 1 整數而非 boolean；統一收成 ``bool | None``。"""
    if value is None:
        return None
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return None


def _first_float(value: Any) -> float | None:
    if not isinstance(value, list) or not value:
        return None
    return _coerce_float(value[0])


def _first_int(value: Any) -> int | None:
    if not isinstance(value, list) or not value:
        return None
    return _coerce_int(value[0])


class NullWeatherProvider(WeatherContextPort):
    """Empty-string provider used when weather is disabled or in tests.

    Mirrors :class:`NullCalendarProvider`'s "always empty" contract so
    call sites can splice the block in unconditionally.
    """

    async def describe(  # pragma: no cover - trivial
        self,
        *,
        now: datetime | None = None,
        location: WeatherLocation | None = None,
    ) -> str:
        _ = now, location
        return ""


def _cache_key(location: WeatherLocation) -> tuple[float, float, str]:
    return (
        round(location.latitude, 3),
        round(location.longitude, 3),
        location.timezone_id,
    )
