"""G2 — a cloud login from a new country hints, it does not relocate (G-4).

The old behaviour only seeded a location when the profile had none, so a
player who moved abroad kept a stale world forever. The new behaviour
records a suggestion; the profile is still never rewritten by a login,
because the stored value may be a deliberate manual choice.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.auth import router as auth_router
from kokoro_link.application.services.player_locale_service import (
    PlayerLocaleService,
)
from kokoro_link.contracts.cloud_auth import CloudProfileSeed
from kokoro_link.contracts.geo_location import GeoLocation
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)

USER_ID = "cloud:acct-hosted"
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _operator() -> OperatorProfile:
    return OperatorProfile(
        id=USER_ID,
        display_name="Hosted Player",
        email="player@example.test",
        primary_language="zh-TW",
        timezone_id="Asia/Taipei",
        country_code="TW",
        latitude=25.03,
        longitude=121.56,
        location_label="Taipei, TW",
        cloud_account_id="acct-hosted",
        cloud_tenant_id="tenant-hosted",
        auth_provider="cloud",
    )


class _Strategy:
    async def login_with_cloud_play_code(
        self, *, code: str, profile_seed: CloudProfileSeed | None = None,
    ) -> tuple[OperatorProfile, str]:
        return _operator(), "core-token"


class _GeoProvider:
    def __init__(self, location: GeoLocation | None) -> None:
        self._location = location

    async def locate(self, ip: str) -> GeoLocation | None:
        return self._location


class _ExplodingLocaleService:
    """Stands in for a preference-store outage."""

    async def record_location_hint(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("preferences unavailable")


def _build(
    *,
    country: str | None,
    locale_service: object | None = None,
) -> tuple[TestClient, PlayerLocaleService | None]:
    profiles = InMemoryOperatorProfileRepository()
    profiles._profiles[USER_ID] = _operator()  # noqa: SLF001 - test seam
    service = locale_service
    if service is None:
        service = PlayerLocaleService(
            profiles=profiles,
            preferences=InMemoryPreferencesRepository(),
            clock=lambda: NOW,
        )
    location = (
        GeoLocation(
            country_code=country,
            latitude=35.68,
            longitude=139.76,
            label="Tokyo, Japan",
            timezone_id="Asia/Tokyo",
        )
        if country
        else None
    )
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=True)),
        auth_strategy=_Strategy(),
        geo_location_provider=_GeoProvider(location),
        operator_profile_repository=profiles,
        player_locale_service=service,
    )
    return TestClient(app), service


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/cloud/session",
        json={"code": "yhp_entry"},
        headers={"x-forwarded-for": "203.0.113.7"},
    )
    assert response.status_code == 200


def test_a_login_from_a_new_country_leaves_a_hint() -> None:
    client, service = _build(country="JP")

    _login(client)

    hint = asyncio.run(service.get_location_hint(USER_ID))
    assert hint is not None
    assert hint.country_code == "JP"


def test_the_profile_is_never_rewritten_by_the_hint() -> None:
    client, _ = _build(country="JP")
    profiles = client.app.state.container.operator_profile_repository

    _login(client)

    stored = asyncio.run(profiles.get(USER_ID))
    assert stored.country_code == "TW"
    assert stored.location_label == "Taipei, TW"


def test_a_login_from_the_same_country_leaves_no_hint() -> None:
    client, service = _build(country="TW")

    _login(client)

    assert asyncio.run(service.get_location_hint(USER_ID)) is None


def test_a_failed_geoip_lookup_does_not_break_login() -> None:
    client, service = _build(country=None)

    _login(client)

    assert asyncio.run(service.get_location_hint(USER_ID)) is None


def test_a_preference_store_outage_does_not_break_login() -> None:
    client, _ = _build(country="JP", locale_service=_ExplodingLocaleService())

    _login(client)
