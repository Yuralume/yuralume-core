"""Open-Meteo geocoding adapter (G2).

Chosen because it is the same family as the weather provider already in
the stack, needs no API key, and returns localised place names via the
``language`` parameter — so a zh-TW player searching "台北" gets 台北市
rather than "Taipei".

Everything here is fail-soft: the caller is a player typing in a search
box, and an upstream hiccup must degrade to "no suggestions", never to
an error toast or a 5xx.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from kokoro_link.contracts.geocoding import GeocodedPlace, GeocodingPort

_LOGGER = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT_TIMEOUT_SECONDS = 4.0
DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 10
MIN_QUERY_LENGTH = 2
"""Open-Meteo itself rejects a single character; short-circuit locally so a
keystroke-per-request UI never spends a round trip on it."""


class OpenMeteoGeocodingClient(GeocodingPort):
    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = (endpoint or DEFAULT_ENDPOINT).strip()
        self._timeout_seconds = max(0.5, float(timeout_seconds))
        self._http = http_client or httpx.AsyncClient()
        # Only a client we created is ours to close; an injected one belongs
        # to the caller (tests, shared pools) and must outlive this adapter.
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def search(
        self,
        query: str,
        *,
        language: str | None = None,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> list[GeocodedPlace]:
        name = (query or "").strip()
        if len(name) < MIN_QUERY_LENGTH:
            return []
        count = max(1, min(int(limit or DEFAULT_RESULT_LIMIT), MAX_RESULT_LIMIT))
        params: dict[str, Any] = {"name": name, "count": count, "format": "json"}
        language_code = _language_parameter(language)
        if language_code:
            params["language"] = language_code
        try:
            response = await self._http.get(
                self._endpoint,
                params=params,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - fail-soft fact provider
            _LOGGER.info("Geocoding lookup failed for %r: %s", name, exc)
            return []
        return _parse_search_payload(payload, limit=count)


def _language_parameter(language: str | None) -> str | None:
    """Project a BCP 47 content-language tag onto Open-Meteo's language code.

    The API takes a bare two-letter code, so ``zh-TW`` becomes ``zh`` and
    ``en-US`` becomes ``en``. An unrecognisable value is dropped rather than
    forwarded — the API then answers in English, which is a better outcome
    than a rejected request.
    """
    head = (language or "").strip().split("-", 1)[0].lower()
    if len(head) != 2 or not head.isalpha():
        return None
    return head


def _parse_search_payload(payload: Any, *, limit: int) -> list[GeocodedPlace]:
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        # Open-Meteo omits ``results`` entirely when nothing matched.
        return []
    places: list[GeocodedPlace] = []
    for raw in raw_results[:limit]:
        place = _parse_result(raw)
        if place is not None:
            places.append(place)
    return places


def _parse_result(raw: Any) -> GeocodedPlace | None:
    if not isinstance(raw, dict):
        return None
    latitude = _coerce_float(raw.get("latitude"))
    longitude = _coerce_float(raw.get("longitude"))
    if latitude is None or longitude is None:
        return None
    try:
        return GeocodedPlace(
            label=_compose_label(raw),
            latitude=latitude,
            longitude=longitude,
            country_code=_coerce_str(raw.get("country_code")),
            timezone_id=_coerce_str(raw.get("timezone")),
        )
    except ValueError:
        return None


def _compose_label(raw: dict[str, Any]) -> str:
    """Build a disambiguating label: city, region, country.

    Open-Meteo returns several same-named towns; without the admin区 and
    country the player cannot tell them apart in the picker.
    """
    parts = [
        _coerce_str(raw.get("name")),
        _coerce_str(raw.get("admin1")),
        _coerce_str(raw.get("country")),
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        if not part or part in seen:
            continue
        seen.add(part)
        ordered.append(part)
    return ", ".join(ordered)


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
