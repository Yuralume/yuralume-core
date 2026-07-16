"""Env → app_runtime_settings bridge (CORE_ENV_TO_ADMIN_CONFIG track 2).

Two responsibilities, both keeping env as the *fallback* and *first-boot
seed* while the DB becomes the source of truth:

* :func:`env_default_for_group` — turn the env-derived :class:`AppSettings`
  value objects into the matching runtime schema, so consumers and the
  admin GET route have a sane default when a group's DB row is absent.
* :func:`seed_app_runtime_settings` — first-boot seed (DB empty → write
  env values), called from the app lifespan next to the provider seed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from pydantic import BaseModel

from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsService,
)
from kokoro_link.bootstrap.settings import (
    AppSettings,
    CalendarSettings,
    GeoIpSettings,
    NsfwModeSettings,
    WeatherSettings,
    WorldEventSettings,
)
from kokoro_link.infrastructure.app_runtime_settings.schemas import (
    CalendarRuntimeConfig,
    CharacterFreezeRuntimeConfig,
    FusionMaterialRuntimeConfig,
    GeoIpRuntimeConfig,
    NsfwRuntimeConfig,
    WeatherRuntimeConfig,
    WorldEventRuntimeConfig,
)

_LOGGER = logging.getLogger(__name__)


def _weather_default(settings: AppSettings) -> WeatherRuntimeConfig:
    w = settings.weather
    return WeatherRuntimeConfig(
        enabled=w.enabled,
        latitude=w.latitude,
        longitude=w.longitude,
        location_label=w.location_label,
        timezone_id=w.timezone_id,
        cache_ttl_seconds=w.cache_ttl_seconds,
    )


def _calendar_default(settings: AppSettings) -> CalendarRuntimeConfig:
    c = settings.calendar
    return CalendarRuntimeConfig(enabled=c.enabled, region=c.region)


def _geoip_default(settings: AppSettings) -> GeoIpRuntimeConfig:
    g = settings.geoip
    return GeoIpRuntimeConfig(
        enabled=g.enabled,
        provider=g.provider,
        endpoint=g.endpoint,
        cache_ttl_seconds=g.cache_ttl_seconds,
        timeout_seconds=g.timeout_seconds,
    )


def _nsfw_default(settings: AppSettings) -> NsfwRuntimeConfig:
    return NsfwRuntimeConfig(ttl_seconds=settings.nsfw_mode.ttl_seconds)


def _world_events_default(settings: AppSettings) -> WorldEventRuntimeConfig:
    we = settings.world_events
    return WorldEventRuntimeConfig(
        retention_days=we.retention_days,
        scheduler_interval_seconds=we.scheduler_interval_seconds,
    )


def _character_freeze_default(settings: AppSettings) -> CharacterFreezeRuntimeConfig:
    # No env source — this is a DB-only site setting introduced with the
    # freeze feature. Seed the schema default (auto-freeze off) so the
    # group exists as a first-class row; the admin console owns it after.
    return CharacterFreezeRuntimeConfig()


def _fusion_material_default(settings: AppSettings) -> FusionMaterialRuntimeConfig:
    # No env source — DB-only site setting introduced with the fusion
    # material-richness badge (Creator Studio C1-P1). Seed the schema
    # default so the group exists as a first-class row; the admin console
    # owns the thresholds after.
    return FusionMaterialRuntimeConfig()


_GROUP_DEFAULT_BUILDERS = {
    "weather": _weather_default,
    "calendar": _calendar_default,
    "geoip": _geoip_default,
    "nsfw": _nsfw_default,
    "world_events": _world_events_default,
    "character_freeze": _character_freeze_default,
    "fusion_material": _fusion_material_default,
}


def env_default_for_group(
    group: str, settings: AppSettings | None,
) -> BaseModel | None:
    """Env-derived default config for a group, or ``None`` if unknown/no settings."""
    if settings is None:
        return None
    builder = _GROUP_DEFAULT_BUILDERS.get(group)
    if builder is None:
        return None
    return builder(settings)


async def seed_app_runtime_settings(
    service: AppRuntimeSettingsService,
    settings: AppSettings,
) -> None:
    """First-boot seed every group from env when its DB row is absent."""
    for group, builder in _GROUP_DEFAULT_BUILDERS.items():
        try:
            seeded = await service.seed_if_absent(group, builder(settings))
            if seeded:
                _LOGGER.info("app_runtime_settings seeded %s from env", group)
        except Exception as exc:
            _LOGGER.warning(
                "app_runtime_settings seed skipped %s: %s", group, exc,
            )


def _apply_overrides(
    settings: AppSettings, groups: dict[str, BaseModel],
) -> AppSettings:
    """Overlay DB-resolved group configs onto env-derived value objects."""
    changes: dict[str, object] = {}
    weather = groups.get("weather")
    if isinstance(weather, WeatherRuntimeConfig):
        changes["weather"] = WeatherSettings(
            enabled=weather.enabled,
            latitude=weather.latitude,
            longitude=weather.longitude,
            location_label=weather.location_label,
            timezone_id=weather.timezone_id,
            cache_ttl_seconds=weather.cache_ttl_seconds,
        )
    calendar = groups.get("calendar")
    if isinstance(calendar, CalendarRuntimeConfig):
        changes["calendar"] = CalendarSettings(
            region=calendar.region, enabled=calendar.enabled,
        )
    geoip = groups.get("geoip")
    if isinstance(geoip, GeoIpRuntimeConfig):
        changes["geoip"] = GeoIpSettings(
            enabled=geoip.enabled,
            provider=geoip.provider,
            endpoint=geoip.endpoint,
            cache_ttl_seconds=geoip.cache_ttl_seconds,
            timeout_seconds=geoip.timeout_seconds,
        )
    nsfw = groups.get("nsfw")
    if isinstance(nsfw, NsfwRuntimeConfig):
        changes["nsfw_mode"] = NsfwModeSettings(ttl_seconds=nsfw.ttl_seconds)
    world_events = groups.get("world_events")
    if isinstance(world_events, WorldEventRuntimeConfig):
        # feeds themselves come from the rss_sources table; only the policy
        # knobs are DB-overlaid here (retention / interval).
        changes["world_events"] = dataclasses.replace(
            settings.world_events,
            retention_days=world_events.retention_days,
            scheduler_interval_seconds=world_events.scheduler_interval_seconds,
        )
    if not changes:
        return settings
    return dataclasses.replace(settings, **changes)


async def _read_all_groups(
    service: AppRuntimeSettingsService, settings: AppSettings,
) -> dict[str, BaseModel]:
    resolved: dict[str, BaseModel] = {}
    for group, builder in _GROUP_DEFAULT_BUILDERS.items():
        resolved[group] = await service.get(group, default=builder(settings))
    return resolved


def overlay_site_settings_from_db(settings: AppSettings) -> AppSettings:
    """Return ``settings`` overlaid with DB-persisted site-settings groups.

    Runs a short-lived async read against the ``app_runtime_settings`` KV
    using its own engine (container build is synchronous and runs outside
    an event loop). Fail-soft: any error (no table yet on a fresh DB,
    transport failure) keeps the env-derived settings unchanged so the app
    still boots."""
    if not settings.database_url:
        return settings
    try:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine,
            build_session_factory,
        )
        from kokoro_link.infrastructure.persistence.sa_runtime_settings_repository import (
            SARuntimeSettingsRepository,
        )

        async def _run() -> dict[str, BaseModel]:
            engine = build_async_engine(settings.database_url)
            try:
                factory = build_session_factory(engine)
                repo = SARuntimeSettingsRepository(factory)
                service = AppRuntimeSettingsService(repo)
                return await _read_all_groups(service, settings)
            finally:
                await engine.dispose()

        groups = asyncio.run(_run())
    except Exception as exc:  # fail-soft — keep env settings, still boot
        _LOGGER.warning(
            "site settings DB overlay skipped (using env defaults): %s", exc,
        )
        return settings
    return _apply_overrides(settings, groups)


__all__ = [
    "env_default_for_group",
    "overlay_site_settings_from_db",
    "seed_app_runtime_settings",
]
