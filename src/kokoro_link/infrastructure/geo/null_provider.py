"""Null GeoLocationPort implementation."""

from __future__ import annotations

from kokoro_link.contracts.geo_location import GeoLocation


class NullGeoLocationProvider:
    async def locate(self, ip: str) -> GeoLocation | None:
        return None
