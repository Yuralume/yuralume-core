"""Tests for site-level runtime settings (CORE_ENV_TO_ADMIN_CONFIG track 2).

Covers the KV-backed AppRuntimeSettingsService (get/set/seed with pydantic
validation), the env→schema defaults + overlay, and the admin routes.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsError,
    AppRuntimeSettingsService,
)
from kokoro_link.bootstrap.app_runtime_settings_seed import (
    _apply_overrides,
    env_default_for_group,
    seed_app_runtime_settings,
)
from kokoro_link.bootstrap.settings import AppSettings, WeatherSettings
from kokoro_link.infrastructure.app_runtime_settings.schemas import (
    WeatherRuntimeConfig,
    key_for_group,
)
from kokoro_link.infrastructure.repositories.in_memory_runtime_settings import (
    InMemoryRuntimeSettingsRepository,
)


def _service() -> AppRuntimeSettingsService:
    return AppRuntimeSettingsService(InMemoryRuntimeSettingsRepository())


@pytest.mark.asyncio
async def test_get_returns_default_when_absent() -> None:
    service = _service()
    default = WeatherRuntimeConfig(latitude=25.0, longitude=121.0)
    got = await service.get("weather", default=default)
    assert got.latitude == 25.0
    assert got.longitude == 121.0


@pytest.mark.asyncio
async def test_set_then_get_roundtrips() -> None:
    service = _service()
    saved = await service.set(
        "weather",
        {"enabled": True, "latitude": 35.0, "longitude": 139.0},
    )
    assert saved.latitude == 35.0
    got = await service.get("weather")
    assert got.longitude == 139.0


@pytest.mark.asyncio
async def test_set_rejects_lat_without_lon() -> None:
    service = _service()
    with pytest.raises(AppRuntimeSettingsError):
        await service.set("weather", {"latitude": 25.0})


@pytest.mark.asyncio
async def test_set_rejects_ttl_below_floor() -> None:
    service = _service()
    with pytest.raises(AppRuntimeSettingsError):
        await service.set("nsfw", {"ttl_seconds": 5})


@pytest.mark.asyncio
async def test_unknown_group_raises() -> None:
    service = _service()
    with pytest.raises(AppRuntimeSettingsError):
        await service.get("does_not_exist")


@pytest.mark.asyncio
async def test_seed_if_absent_only_seeds_once() -> None:
    repo = InMemoryRuntimeSettingsRepository()
    service = AppRuntimeSettingsService(repo)
    cfg = WeatherRuntimeConfig(latitude=1.0, longitude=2.0)
    assert await service.seed_if_absent("weather", cfg) is True
    # Second call is a no-op (DB no longer empty for that group).
    other = WeatherRuntimeConfig(latitude=9.0, longitude=9.0)
    assert await service.seed_if_absent("weather", other) is False
    got = await service.get("weather")
    assert got.latitude == 1.0  # original seed preserved


@pytest.mark.asyncio
async def test_seed_app_runtime_settings_populates_all_groups() -> None:
    repo = InMemoryRuntimeSettingsRepository()
    service = AppRuntimeSettingsService(repo)
    settings = AppSettings(
        weather=WeatherSettings(latitude=25.0, longitude=121.0),
    )
    await seed_app_runtime_settings(service, settings)
    for group in ("weather", "calendar", "geoip", "nsfw", "world_events"):
        assert await repo.get(key_for_group(group)) is not None


def test_env_default_for_group_maps_weather() -> None:
    settings = AppSettings(weather=WeatherSettings(latitude=10.0, longitude=20.0))
    default = env_default_for_group("weather", settings)
    assert isinstance(default, WeatherRuntimeConfig)
    assert default.latitude == 10.0
    # Unset label stays empty — no hardcoded Chinese leaks into the admin form.
    assert default.location_label == ""


def test_weather_runtime_config_defaults_to_blank_label() -> None:
    assert WeatherRuntimeConfig().location_label == ""


def test_weather_runtime_config_self_heals_legacy_chinese_label() -> None:
    # Older installs seeded the literal "目前位置"; reading it back must
    # normalise to "" so an en/ja admin form no longer shows Chinese.
    cfg = WeatherRuntimeConfig(location_label="目前位置")
    assert cfg.location_label == ""
    # A real user-entered label is preserved untouched.
    assert WeatherRuntimeConfig(location_label="台北市").location_label == "台北市"


def test_apply_overrides_swaps_weather() -> None:
    settings = AppSettings()
    overlaid = _apply_overrides(
        settings,
        {"weather": WeatherRuntimeConfig(latitude=48.0, longitude=2.0)},
    )
    assert overlaid.weather.latitude == 48.0
    assert overlaid.weather.longitude == 2.0
    # Untouched groups keep their env objects.
    assert overlaid.calendar == settings.calendar


# ---------------------------------------------------------------------------
# Admin routes (GET catalog / GET group / PUT group). No-auth in-memory app.
# ---------------------------------------------------------------------------


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-app-settings-key")


def test_admin_app_settings_catalog_lists_groups(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())
    response = client.get("/api/v1/admin/app-settings")
    assert response.status_code == 200
    groups = {g["group"] for g in response.json()["groups"]}
    assert {"weather", "calendar", "geoip", "nsfw", "world_events"} <= groups


def test_admin_app_settings_put_then_get(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/admin/app-settings/weather",
        json={"enabled": True, "latitude": 40.0, "longitude": -74.0},
    )
    assert put.status_code == 200
    assert put.json()["values"]["latitude"] == 40.0

    got = client.get("/api/v1/admin/app-settings/weather")
    assert got.status_code == 200
    assert got.json()["values"]["longitude"] == -74.0


def test_admin_app_settings_put_rejects_invalid(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())
    resp = client.put(
        "/api/v1/admin/app-settings/weather",
        json={"latitude": 40.0},  # lon missing → 400
    )
    assert resp.status_code == 400


def test_admin_app_settings_unknown_group_404(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())
    resp = client.get("/api/v1/admin/app-settings/bogus")
    assert resp.status_code == 404
