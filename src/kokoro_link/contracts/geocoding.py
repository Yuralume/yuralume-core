"""Place-name geocoding port (G2).

Hosted players pick "where I am" by typing a city name, not by entering
latitude / longitude. This port turns a free-text place name into the
coordinate + region + timezone facts the operator profile stores.

It is a **fact provider**: every failure mode (upstream down, timeout,
garbage payload) degrades to an empty list, never an exception. A player
who gets no suggestions can still fall back to the existing manual
location fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class GeocodedPlace:
    """One place suggestion, already shaped for the operator profile."""

    label: str
    """Human-readable, already-localised place name (``台北市, 臺灣``)."""
    latitude: float
    longitude: float
    country_code: str | None = None
    timezone_id: str | None = None
    """IANA id suggested by the provider; the consumer still normalises it."""

    def __post_init__(self) -> None:
        label = (self.label or "").strip()
        if not label:
            raise ValueError("GeocodedPlace.label must be non-empty")
        object.__setattr__(self, "label", label)
        lat = float(self.latitude)
        lon = float(self.longitude)
        if lat < -90.0 or lat > 90.0:
            raise ValueError(f"invalid latitude: {self.latitude!r}")
        if lon < -180.0 or lon > 180.0:
            raise ValueError(f"invalid longitude: {self.longitude!r}")
        object.__setattr__(self, "latitude", lat)
        object.__setattr__(self, "longitude", lon)
        country = (self.country_code or "").strip().upper()
        if country and (len(country) != 2 or not country.isalpha()):
            raise ValueError(f"invalid country code: {self.country_code!r}")
        object.__setattr__(self, "country_code", country or None)
        timezone_id = (self.timezone_id or "").strip()
        object.__setattr__(self, "timezone_id", timezone_id or None)


class GeocodingPort(Protocol):
    async def search(
        self,
        query: str,
        *,
        language: str | None = None,
        limit: int = 5,
    ) -> list[GeocodedPlace]:
        """Return place suggestions for ``query``.

        An empty list means "nothing to offer" for any reason — blank or
        too-short query, upstream failure, timeout. Callers must not treat
        it as an error.
        """
