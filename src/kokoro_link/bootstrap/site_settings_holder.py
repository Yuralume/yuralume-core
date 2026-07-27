"""Hot-swappable holder for the "real world" site settings (G0).

``build_container`` reads the DB-persisted ``site.*`` groups once, overlays them
onto the env-derived :class:`AppSettings`, and hands frozen value objects to
every consumer. That is correct for self-host (one process, restarted on every
change) and wrong for Hosted: 2×api + coordinator + worker each captured
whatever the table said at *its own* boot, so an Admin change to the site
weather coordinates took effect on one replica and needed a rolling restart to
reach the rest (HOSTED_PLAYER_GEO_ADAPTATION_PLAN §0.2 G-2).

This module is the seam that makes that reloadable *without* rebuilding the
whole settings system. It holds one immutable
:class:`SiteSettingsSnapshot` behind a single reference, and consumers read it
through accessors instead of capturing a value at wiring time.

**Scope is deliberately narrow** (owner decision, §7-5): only the four groups
that carry real-world information a player's characters perceive —
``weather`` / ``calendar`` / ``geoip`` / ``world_events``.

.. todo::

   The remaining ``app_runtime_settings`` groups (``nsfw``,
   ``character_freeze``, ``fusion_material``) are still boot snapshots read off
   ``AppSettings``/their own services. They are site-operator policy knobs
   rather than per-player world facts, so a restart-to-apply is tolerable;
   extend :data:`SITE_SETTINGS_HOT_GROUPS` and this snapshot when that stops
   being true. Note that adding a group here is only half the job — its
   consumer has to read through an accessor too, or the holder just holds a
   value nobody looks at.

Thread/async safety: replacement is a single attribute rebind of an immutable
snapshot, which is atomic under CPython's GIL. A reader therefore always sees
either the whole old snapshot or the whole new one — never a half-applied mix —
with no lock on the read path, which matters because the read path is every
weather/calendar lookup in the chat and scheduler hot loops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kokoro_link.bootstrap.settings import (
    AppSettings,
    CalendarSettings,
    GeoIpSettings,
    WeatherSettings,
    WorldEventSettings,
)

SITE_SETTINGS_HOT_GROUPS: tuple[str, ...] = (
    "weather",
    "calendar",
    "geoip",
    "world_events",
)
"""``app_runtime_settings`` groups this holder tracks and reloads."""

OverlaySource = Literal["db", "env_fallback"]
"""Where the current snapshot's values came from.

``env_fallback`` means the DB overlay did not happen — either there is no
database at all (in-memory / test containers) or the read failed and the
fail-soft path kept the env-derived defaults. The latter used to be invisible;
it is surfaced on ``/health`` so an operator can tell "my Admin change had no
effect" apart from "my Admin change was never read".
"""


@dataclass(frozen=True, slots=True)
class SiteSettingsSnapshot:
    """One coherent set of the four hot groups, plus its provenance."""

    weather: WeatherSettings
    calendar: CalendarSettings
    geoip: GeoIpSettings
    world_events: WorldEventSettings
    source: OverlaySource = "db"

    @classmethod
    def from_app_settings(
        cls, settings: AppSettings, *, source: OverlaySource = "db",
    ) -> "SiteSettingsSnapshot":
        """Project the four hot groups out of a (possibly overlaid) AppSettings."""
        return cls(
            weather=settings.weather,
            calendar=settings.calendar,
            geoip=settings.geoip,
            world_events=settings.world_events,
            source=source,
        )


class SiteSettingsHolder:
    """One process's current site settings, atomically replaceable."""

    __slots__ = ("_snapshot",)

    def __init__(self, snapshot: SiteSettingsSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> SiteSettingsSnapshot:
        """The whole current snapshot (one read → one coherent set)."""
        return self._snapshot

    def replace(self, snapshot: SiteSettingsSnapshot) -> None:
        """Swap in a new snapshot. Single rebind — see the module docstring."""
        self._snapshot = snapshot

    # --- accessors handed to consumers instead of captured values ----------

    def weather(self) -> WeatherSettings:
        return self._snapshot.weather

    def calendar(self) -> CalendarSettings:
        return self._snapshot.calendar

    def geoip(self) -> GeoIpSettings:
        return self._snapshot.geoip

    def world_events(self) -> WorldEventSettings:
        return self._snapshot.world_events

    def calendar_region(self) -> str:
        """The site calendar region code — the deployment's own region.

        Its own accessor because the RSS seed binds region-scoped emergency
        feeds to it and only needs that one string.
        """
        return self._snapshot.calendar.region

    @property
    def overlay_source(self) -> OverlaySource:
        return self._snapshot.source


__all__ = [
    "SITE_SETTINGS_HOT_GROUPS",
    "OverlaySource",
    "SiteSettingsHolder",
    "SiteSettingsSnapshot",
]
