"""IP geolocation port.

This port resolves an incoming client IP to coarse geographic facts used
as the operator's editable location seed. It returns facts only; callers
store them on ``OperatorProfile`` and later fact providers decide how to
use that location.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class GeoLocation:
    country_code: str
    latitude: float
    longitude: float
    label: str
    timezone_id: str | None = None
    """IANA timezone id resolved from the IP (e.g. ``Asia/Taipei``).

    Optional because not every geo backend supplies it and callers must
    treat its absence as "unknown" rather than an error. It is stored
    verbatim (only trimmed) here; the consumer normalises / validates it
    against the IANA database before pinning it on an operator profile.
    """

    def __post_init__(self) -> None:
        country = (self.country_code or "").strip().upper()
        if len(country) != 2 or not country.isalpha():
            raise ValueError(f"invalid country code: {self.country_code!r}")
        object.__setattr__(self, "country_code", country)
        lat = float(self.latitude)
        lon = float(self.longitude)
        if lat < -90.0 or lat > 90.0:
            raise ValueError(f"invalid latitude: {self.latitude!r}")
        if lon < -180.0 or lon > 180.0:
            raise ValueError(f"invalid longitude: {self.longitude!r}")
        object.__setattr__(self, "latitude", lat)
        object.__setattr__(self, "longitude", lon)
        label = (self.label or "").strip()
        if not label:
            label = country
        object.__setattr__(self, "label", label)
        timezone_id = (self.timezone_id or "").strip()
        object.__setattr__(self, "timezone_id", timezone_id or None)


class GeoLocationPort(Protocol):
    async def locate(self, ip: str) -> GeoLocation | None:
        """Return coarse location facts for ``ip``.

        ``None`` means unavailable, disabled, private/local IP, or
        upstream failure. Callers must not treat this as an error.
        """
