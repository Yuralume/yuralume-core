"""Pydantic schemas for site-level runtime settings (CORE_ENV_TO_ADMIN_CONFIG track 2).

Each *group* is one small JSON blob persisted under a single key in the
existing ``app_runtime_settings`` KV table (HUMANIZATION_ROADMAP §4.5).
The consumer deserialises the blob into the matching schema, so type +
cross-field validation (lat/lon pairing, TTL floors) lives here rather
than in DB constraints — the plan's option A (key-value + pydantic).

The env-driven :class:`AppSettings` value objects remain the *fallback*
source and the first-boot seed; these schemas are the DB-backed shape the
Admin UI reads and writes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class WeatherRuntimeConfig(BaseModel):
    """Real-world weather facts (Open-Meteo). lat/lon must come as a pair."""

    enabled: bool = True
    latitude: float | None = None
    longitude: float | None = None
    location_label: str = ""
    timezone_id: str = "auto"
    cache_ttl_seconds: int = Field(default=15 * 60, ge=60)

    @model_validator(mode="after")
    def _normalise_location_label(self) -> "WeatherRuntimeConfig":
        # Upgrade self-heal: older installs seeded the hardcoded Chinese
        # default "目前位置" into app_runtime_settings, which then leaked into
        # the admin form on non-Chinese deployments. Because this exact value
        # is the legacy default (and equals the zh-TW localized fallback
        # anyway), coercing it to "" is safe — the empty value re-resolves to
        # the viewer's localized label downstream, and zh-TW users still see
        # "目前位置" via the localized fallback.
        if (self.location_label or "").strip() == "目前位置":
            object.__setattr__(self, "location_label", "")
        return self

    @model_validator(mode="after")
    def _lat_lon_paired(self) -> "WeatherRuntimeConfig":
        # One-without-the-other is a config error: Open-Meteo needs both.
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError(
                "latitude and longitude must be provided together",
            )
        if self.latitude is not None and not -90.0 <= self.latitude <= 90.0:
            raise ValueError("latitude must be within [-90, 90]")
        if self.longitude is not None and not -180.0 <= self.longitude <= 180.0:
            raise ValueError("longitude must be within [-180, 180]")
        return self


class CalendarRuntimeConfig(BaseModel):
    """Region-holiday calendar facts. ``region`` is a ``holidays`` code."""

    enabled: bool = True
    region: str = "TW"

    @model_validator(mode="after")
    def _normalise_region(self) -> "CalendarRuntimeConfig":
        object.__setattr__(self, "region", (self.region or "TW").strip().upper())
        return self


class GeoIpRuntimeConfig(BaseModel):
    """IP-based location seed for new operator profiles (public service, no auth)."""

    enabled: bool = True
    provider: str = "ip-api"
    endpoint: str = "http://ip-api.com/json/"
    cache_ttl_seconds: int = Field(default=24 * 60 * 60, ge=60)
    timeout_seconds: float = Field(default=3.0, ge=0.5)


class NsfwRuntimeConfig(BaseModel):
    """Per-user NSFW mode idle expiry (seconds)."""

    ttl_seconds: int = Field(default=30 * 60, ge=60)


class WorldEventRuntimeConfig(BaseModel):
    """World-event pipeline policy knobs (feeds themselves live in a table)."""

    retention_days: int = Field(default=30, ge=1)
    scheduler_interval_seconds: float = Field(default=3600.0, ge=60.0)


class FusionMaterialRuntimeConfig(BaseModel):
    """Richness thresholds for the fusion character-picker material badge
    (CREATOR_STUDIO_VALUE_LINE_PLAN §2.1-5, Creator Studio C1-P1).

    A character's badge tier (``rich`` / ``ok`` / ``sparse``) is decided
    by deterministic bookkeeping over the salience-ranked memory slice the
    fusion brief actually pulls: the count of chosen memories and their
    total length. These knobs let an operator tune where the thresholds
    sit without a code change — the classification numbers live only here
    and in the persisted DB row, never hard-coded in the service or UI.

    They never *block* creation. A ``sparse`` character only prompts the
    picker to show a soft, positive nudge to chat more before fusing, so
    the story carries the pair's shared memories instead of degrading into
    generic AI writing."""

    ok_min_count: int = Field(default=3, ge=0)
    ok_min_chars: int = Field(default=300, ge=0)
    rich_min_count: int = Field(default=8, ge=0)
    rich_min_chars: int = Field(default=1000, ge=0)


class CharacterFreezeRuntimeConfig(BaseModel):
    """Auto-freeze policy for dormant characters (CHARACTER_FREEZE_PLAN).

    Cost-control knob: a character that has had no user interaction for
    ``idle_days_threshold`` civil days is automatically frozen by the
    idle-sweep reaper, halting all of its background scheduler activity
    while preserving its state. Foreground chat auto-unfreezes it.

    ``auto_freeze_enabled`` defaults to ``False`` (opt-in) so upgrading
    an existing install never silently freezes characters — the operator
    must turn it on. Immediate per-character freeze from the admin
    console works regardless of this flag."""

    auto_freeze_enabled: bool = False
    idle_days_threshold: int = Field(default=30, ge=1)


# Group registry: maps the admin route ``{group}`` segment + KV key to its
# schema. Kept as one table so routes / seeding / consumers all agree on
# the key namespace without stringly-typed drift.
APP_SETTINGS_GROUPS: dict[str, type[BaseModel]] = {
    "weather": WeatherRuntimeConfig,
    "calendar": CalendarRuntimeConfig,
    "geoip": GeoIpRuntimeConfig,
    "nsfw": NsfwRuntimeConfig,
    "world_events": WorldEventRuntimeConfig,
    "character_freeze": CharacterFreezeRuntimeConfig,
    "fusion_material": FusionMaterialRuntimeConfig,
}

# The KV key each group persists under. Prefixed to avoid colliding with
# other app_runtime_settings users (e.g. quiet_hours_*).
APP_SETTINGS_KEY_PREFIX = "site."


def key_for_group(group: str) -> str:
    return f"{APP_SETTINGS_KEY_PREFIX}{group}"


__all__ = [
    "APP_SETTINGS_GROUPS",
    "APP_SETTINGS_KEY_PREFIX",
    "CalendarRuntimeConfig",
    "CharacterFreezeRuntimeConfig",
    "FusionMaterialRuntimeConfig",
    "GeoIpRuntimeConfig",
    "NsfwRuntimeConfig",
    "WeatherRuntimeConfig",
    "WorldEventRuntimeConfig",
    "key_for_group",
]
