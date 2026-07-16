"""ip-api.com backed GeoLocationPort implementation."""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from kokoro_link.contracts.geo_location import GeoLocation, GeoLocationPort

_LOGGER = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://ip-api.com/json/"
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60


class IpApiGeoLocationProvider(GeoLocationPort):
    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = (endpoint or DEFAULT_ENDPOINT).strip()
        self._timeout_seconds = max(0.5, float(timeout_seconds))
        self._cache_ttl_seconds = max(60, int(cache_ttl_seconds))
        self._http = http_client or httpx.AsyncClient()
        self._cache: dict[str, tuple[datetime, GeoLocation | None]] = {}

    async def locate(self, ip: str) -> GeoLocation | None:
        clean_ip = _normalise_public_ip(ip)
        if clean_ip is None:
            return None
        now = datetime.now(timezone.utc)
        cached = self._cache.get(clean_ip)
        if cached is not None:
            cached_at, location = cached
            if now - cached_at < timedelta(seconds=self._cache_ttl_seconds):
                return location
        try:
            response = await self._http.get(
                _ip_api_lookup_url(self._endpoint, clean_ip),
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            location = _parse_ip_api_payload(payload)
        except Exception as exc:  # noqa: BLE001 - fail-soft fact provider
            _LOGGER.info(
                "GeoIP lookup failed for %s; leaving operator location empty: %s",
                clean_ip,
                exc,
            )
            location = None
        self._cache[clean_ip] = (now, location)
        return location


def _normalise_public_ip(raw: str) -> str | None:
    candidate = (raw or "").strip()
    if not candidate:
        return None
    if "," in candidate:
        candidate = candidate.split(",", 1)[0].strip()
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return None
    return str(ip)


def _ip_api_lookup_url(endpoint: str, ip: str) -> str:
    base = (endpoint or DEFAULT_ENDPOINT).strip().rstrip("/")
    return f"{base}/{quote(ip, safe=':')}"


def _parse_ip_api_payload(payload: Any) -> GeoLocation | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("status", "")).lower() == "fail":
        return None
    country = _coerce_str(payload.get("countryCode"))
    lat = _coerce_float(payload.get("lat"))
    lon = _coerce_float(payload.get("lon"))
    if country is None or lat is None or lon is None:
        return None
    city = _coerce_str(payload.get("city"))
    region = _coerce_str(payload.get("regionName") or payload.get("region"))
    label_parts = [part for part in (city, region, country.upper()) if part]
    label = ", ".join(label_parts) or country.upper()
    timezone_id = _coerce_str(payload.get("timezone"))
    try:
        return GeoLocation(
            country_code=country,
            latitude=lat,
            longitude=lon,
            label=label,
            timezone_id=timezone_id,
        )
    except ValueError:
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
