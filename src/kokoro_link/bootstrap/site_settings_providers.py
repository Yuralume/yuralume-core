"""Site-settings-backed providers that rebuild when the settings move (G0).

The three "real world" adapters (weather / calendar / GeoIP) are configured
entirely from the hot site-settings groups, and every one of them used to be
constructed once from the boot snapshot. This module holds:

* the pure builders — value object in, port out, no ``AppSettings`` and no
  container; and
* a thin *reloadable* wrapper per port that reads the current value object from
  a :class:`SiteSettingsHolder` on each call and rebuilds its inner adapter only
  when that value object actually changed.

Why wrappers rather than mutating the adapters: the adapters own caches keyed to
their configuration (Open-Meteo's payload cache, the ``holidays`` per-region
calendars, the GeoIP IP cache). Rebuilding on change discards exactly the cache
that just became invalid, and the steady state — settings unchanged, the common
case by a very large margin — is one frozen-dataclass equality check per call
and zero allocation.

The check-then-rebuild is not guarded by a lock, and does not need one: it
contains no ``await``, so it cannot interleave within an event loop, and the
worst case under genuine thread parallelism is two adapters built and one
immediately dropped. Never a torn read — the holder hands out an immutable
snapshot (see :mod:`kokoro_link.bootstrap.site_settings_holder`).

**Red line — no per-call database access.** The accessor reads process memory
that the refresher updates out of band. A settings read must never become a
query on the chat hot path.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, tzinfo

from kokoro_link.bootstrap.settings import (
    CalendarSettings,
    GeoIpSettings,
    WeatherSettings,
)
from kokoro_link.bootstrap.site_settings_holder import SiteSettingsHolder
from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.contracts.geo_location import GeoLocation, GeoLocationPort
from kokoro_link.contracts.weather_context import WeatherContextPort, WeatherLocation
from kokoro_link.infrastructure.calendar.holidays_provider import (
    HolidaysCalendarProvider,
    NullCalendarProvider,
)
from kokoro_link.infrastructure.geo.ip_api_provider import IpApiGeoLocationProvider
from kokoro_link.infrastructure.geo.null_provider import NullGeoLocationProvider
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.weather.open_meteo_provider import (
    NullWeatherProvider,
    OpenMeteoWeatherProvider,
)

_LOGGER = logging.getLogger(__name__)


# --- pure builders ---------------------------------------------------------


def build_weather_provider(
    settings: WeatherSettings, *, default_primary_language: str,
) -> WeatherContextPort:
    """Weather adapter for one :class:`WeatherSettings`.

    Falls through to :class:`NullWeatherProvider` when weather is disabled —
    prompt blocks stay empty without any conditional logic at the call sites.
    """
    if not settings.enabled:
        return NullWeatherProvider()
    # When the deployment leaves the label empty, resolve it to the deploy-time
    # content language instead of the provider's hardcoded Chinese last resort,
    # so an en/ja deployment's fallback weather label follows its own language.
    location_label = settings.location_label.strip() or localized_fallback_text(
        "weather.current_location_label", default_primary_language,
    )
    return OpenMeteoWeatherProvider(
        latitude=settings.latitude,
        longitude=settings.longitude,
        location_label=location_label,
        timezone_id=settings.timezone_id,
        cache_ttl_seconds=settings.cache_ttl_seconds,
    )


def build_calendar_provider(
    settings: CalendarSettings, *, local_tz: tzinfo,
) -> CalendarContextPort:
    """Calendar adapter for one :class:`CalendarSettings`."""
    if not settings.enabled:
        return NullCalendarProvider()
    return HolidaysCalendarProvider(region=settings.region, local_tz=local_tz)


def build_geo_location_provider(settings: GeoIpSettings) -> GeoLocationPort:
    """GeoIP adapter for one :class:`GeoIpSettings`."""
    if not settings.enabled or settings.provider != "ip-api":
        return NullGeoLocationProvider()
    return IpApiGeoLocationProvider(
        endpoint=settings.endpoint,
        timeout_seconds=settings.timeout_seconds,
        cache_ttl_seconds=settings.cache_ttl_seconds,
    )


# --- reloadable wrappers ---------------------------------------------------


class ReloadableWeatherProvider(WeatherContextPort):
    """:class:`WeatherContextPort` that follows the live site weather settings."""

    def __init__(
        self,
        *,
        holder: SiteSettingsHolder,
        default_primary_language: str,
    ) -> None:
        self._holder = holder
        self._language = default_primary_language
        self._applied: WeatherSettings | None = None
        self._inner: WeatherContextPort = NullWeatherProvider()

    @property
    def inner(self) -> WeatherContextPort:
        """The adapter currently in force (diagnostics + tests)."""
        return self._current()

    def _current(self) -> WeatherContextPort:
        settings = self._holder.weather()
        if settings != self._applied:
            self._inner = build_weather_provider(
                settings, default_primary_language=self._language,
            )
            self._applied = settings
        return self._inner

    async def describe(
        self,
        *,
        now: datetime | None = None,
        location: WeatherLocation | None = None,
    ) -> str:
        return await self._current().describe(now=now, location=location)


class ReloadableCalendarProvider(CalendarContextPort):
    """:class:`CalendarContextPort` that follows the live site calendar settings."""

    def __init__(self, *, holder: SiteSettingsHolder, local_tz: tzinfo) -> None:
        self._holder = holder
        self._local_tz = local_tz
        self._applied: CalendarSettings | None = None
        self._inner: CalendarContextPort = NullCalendarProvider()

    @property
    def inner(self) -> CalendarContextPort:
        return self._current()

    def _current(self) -> CalendarContextPort:
        settings = self._holder.calendar()
        if settings != self._applied:
            self._inner = build_calendar_provider(
                settings, local_tz=self._local_tz,
            )
            self._applied = settings
        return self._inner

    def describe(self, today: date, *, region: str | None = None) -> str:
        return self._current().describe(today, region=region)


class ReloadableGeoLocationProvider(GeoLocationPort):
    """:class:`GeoLocationPort` that follows the live site GeoIP settings.

    The GeoIP adapter is the one of the three that owns an ``httpx.AsyncClient``
    *and* is awaited on a request path, so it needs both halves of a swap:
    release the superseded adapter (or every settings edit strands a connection
    pool) but not while a lookup is still awaiting it (or the settings edit
    turns concurrent logins into "client closed" errors). Hence the in-flight
    count — releases are deferred until the last lookup holding an old adapter
    returns.
    """

    def __init__(self, *, holder: SiteSettingsHolder) -> None:
        self._holder = holder
        self._applied: GeoIpSettings | None = None
        self._inner: GeoLocationPort = NullGeoLocationProvider()
        self._in_flight = 0
        self._deferred: list[GeoLocationPort] = []

    @property
    def inner(self) -> GeoLocationPort:
        return self._current()

    def _current(self) -> GeoLocationPort:
        settings = self._holder.geoip()
        if settings != self._applied:
            superseded = self._inner
            self._inner = build_geo_location_provider(settings)
            self._applied = settings
            self._retire(superseded)
        return self._inner

    def _retire(self, superseded: GeoLocationPort) -> None:
        """Release now, or park it until the last in-flight lookup returns.

        Once ``_inner`` has been rebound no *new* lookup can reach the old
        adapter, so the only holders left are the calls already counted in
        ``_in_flight``.
        """
        if self._in_flight:
            self._deferred.append(superseded)
            return
        _release(superseded)

    async def locate(self, ip: str) -> GeoLocation | None:
        # Resolve and count before the first ``await``: a single event loop
        # gives no preemption between those two statements, so the adapter this
        # call uses is provably covered by the count that protects it.
        provider = self._current()
        self._in_flight += 1
        try:
            return await provider.locate(ip)
        finally:
            self._in_flight -= 1
            if not self._in_flight and self._deferred:
                parked, self._deferred = self._deferred, []
                for superseded in parked:
                    _release(superseded)


def _release(provider: object) -> None:
    """Best-effort teardown of a superseded adapter's owned HTTP client.

    The GeoIP adapter builds its own long-lived ``httpx.AsyncClient``; rebuilding
    on every settings change would otherwise leak one connection pool per change.
    Synchronous and fire-and-forget on purpose — this runs on a hot read path and
    must never block it, let alone fail it.
    """
    release = getattr(provider, "release", None)
    if release is None:
        return
    try:
        release()
    except Exception:  # noqa: BLE001 — teardown must never break a lookup
        _LOGGER.warning("superseded geo provider teardown failed", exc_info=True)


__all__ = [
    "ReloadableCalendarProvider",
    "ReloadableGeoLocationProvider",
    "ReloadableWeatherProvider",
    "build_calendar_provider",
    "build_geo_location_provider",
    "build_weather_provider",
]
