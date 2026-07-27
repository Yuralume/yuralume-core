"""G2 — accepting a location through the existing profile API retires the hint.

Accepting the relocation suggestion and setting a location by hand are the
same thing from the hint's point of view: the player has answered, so the
prompt must not come back on the next ``/auth/me``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.api.routes.operator import router as operator_router
from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.application.services.player_locale_service import (
    PlayerLocaleService,
)
from kokoro_link.contracts.geo_location import GeoLocation
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)

USER_ID = "cloud:acct-1"
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _operator() -> OperatorProfile:
    return OperatorProfile(
        id=USER_ID,
        display_name="Player",
        primary_language="zh-TW",
        timezone_id="Asia/Taipei",
        country_code="TW",
        location_label="Taipei, TW",
        cloud_account_id="acct-1",
        cloud_tenant_id="tenant-1",
        auth_provider="cloud",
    )


def _build() -> tuple[TestClient, PlayerLocaleService]:
    profiles = InMemoryOperatorProfileRepository()
    operator = _operator()
    profiles._profiles[operator.id] = operator  # noqa: SLF001 - test seam
    locale_service = PlayerLocaleService(
        profiles=profiles,
        preferences=InMemoryPreferencesRepository(),
        clock=lambda: NOW,
    )

    async def _seed_hint() -> None:
        await locale_service.record_location_hint(
            USER_ID,
            location=GeoLocation(
                country_code="JP",
                latitude=35.68,
                longitude=139.76,
                label="Tokyo, Japan",
                timezone_id="Asia/Tokyo",
            ),
            profile=operator,
        )

    asyncio.run(_seed_hint())

    app = FastAPI()
    app.include_router(operator_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=True)),
        operator_profile_service=OperatorProfileService(repository=profiles),
        player_locale_service=locale_service,
    )
    app.dependency_overrides[get_current_user] = lambda: operator
    return TestClient(app), locale_service


def test_updating_the_location_clears_a_pending_hint() -> None:
    client, locale_service = _build()

    response = client.put(
        "/api/v1/operator/profile",
        json={"country_code": "JP", "location_label": "Tokyo, JP"},
    )

    assert response.status_code == 200
    assert asyncio.run(locale_service.get_location_hint(USER_ID)) is None


def test_an_unrelated_profile_edit_leaves_the_hint_alone() -> None:
    client, locale_service = _build()

    response = client.put(
        "/api/v1/operator/profile", json={"display_name": "Renamed"},
    )

    assert response.status_code == 200
    hint = asyncio.run(locale_service.get_location_hint(USER_ID))
    assert hint is not None
    assert hint.country_code == "JP"
