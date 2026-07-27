"""G2 — ``PATCH /auth/me/locale`` + ``POST /auth/me/locale/confirm`` routes.

Cloud-mode-only surfaces (same 404 convention as ``POST
/auth/cloud/session`` / ``GET /cloud/credits``): self-host must see the
exact route inventory it always did, and keep its repair-CLI-only story
for the pinned identity locale.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.api.routes.auth import router as auth_router
from kokoro_link.api.routes.auth_locale import router as auth_locale_router
from kokoro_link.application.services.player_locale_service import (
    LOCALE_CHANGE_LIMIT,
    LOCALE_CLAIM_PREFIX,
    PlayerLocaleService,
)
from kokoro_link.application.services.runtime_claim import RuntimeClaim
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
        email="player@example.test",
        primary_language="zh-TW",
        timezone_id="Asia/Taipei",
        country_code="TW",
        latitude=25.03,
        longitude=121.56,
        location_label="Taipei, TW",
        cloud_account_id="acct-1",
        cloud_tenant_id="tenant-1",
        auth_provider="cloud",
    )


class _FixedClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class _HeldElsewhereLease:
    """Lease port stand-in for "another replica is mid-write on this key"."""

    async def acquire(
        self, name: str, owner_id: str, *, ttl_seconds: int, now: datetime,
    ) -> int | None:
        return None

    async def release(self, name: str, owner_id: str) -> None:
        return None


def _client(
    *,
    cloud_active: bool = True,
    wire_service: bool = True,
    change_claim: RuntimeClaim | None = None,
) -> tuple[TestClient, PlayerLocaleService | None, InMemoryOperatorProfileRepository, _FixedClock]:
    profiles = InMemoryOperatorProfileRepository()
    operator = _operator()
    profiles._profiles[operator.id] = operator  # noqa: SLF001 - test seam
    clock = _FixedClock()
    service = (
        PlayerLocaleService(
            profiles=profiles,
            preferences=InMemoryPreferencesRepository(),
            clock=clock,
            change_claim=change_claim,
        )
        if wire_service
        else None
    )
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(auth_locale_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=cloud_active)),
        player_locale_service=service,
    )
    app.dependency_overrides[get_current_user] = lambda: operator
    return TestClient(app), service, profiles, clock


# ----------------------------------------------------------------------
# mode matrix
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/api/v1/auth/me/locale", {"timezone_id": "Asia/Tokyo"}),
        ("post", "/api/v1/auth/me/locale/confirm", {}),
    ],
)
def test_locale_routes_are_hidden_in_self_host_mode(
    method: str, path: str, body: dict,
) -> None:
    client, _, profiles, _ = _client(cloud_active=False)

    response = getattr(client, method)(path, json=body)

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/api/v1/auth/me/locale", {"timezone_id": "Asia/Tokyo"}),
        ("post", "/api/v1/auth/me/locale/confirm", {}),
    ],
)
def test_locale_routes_degrade_when_the_service_is_unwired(
    method: str, path: str, body: dict,
) -> None:
    client, _, _, _ = _client(wire_service=False)

    response = getattr(client, method)(path, json=body)

    assert response.status_code == 503


# ----------------------------------------------------------------------
# PATCH /auth/me/locale
# ----------------------------------------------------------------------


def test_changing_the_timezone_returns_the_remaining_budget() -> None:
    client, _, profiles, _ = _client()

    response = client.patch(
        "/api/v1/auth/me/locale", json={"timezone_id": "Asia/Tokyo"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user"]["timezone_id"] == "Asia/Tokyo"
    assert payload["remaining_changes"] == LOCALE_CHANGE_LIMIT - 1
    assert payload["change_limit"] == LOCALE_CHANGE_LIMIT


def test_changing_the_language_persists_it() -> None:
    client, _, profiles, _ = _client()

    response = client.patch(
        "/api/v1/auth/me/locale", json={"primary_language": "ja"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["primary_language"] == "ja"


def test_an_empty_body_is_rejected() -> None:
    client, _, _, _ = _client()

    assert client.patch("/api/v1/auth/me/locale", json={}).status_code == 422


def test_an_unknown_timezone_is_rejected() -> None:
    client, _, _, _ = _client()

    response = client.patch(
        "/api/v1/auth/me/locale", json={"timezone_id": "Mars/Olympus"},
    )

    assert response.status_code == 422


def test_a_structurally_broken_language_tag_is_rejected() -> None:
    client, _, _, _ = _client()

    response = client.patch(
        "/api/v1/auth/me/locale", json={"primary_language": "123"},
    )

    assert response.status_code == 422


def test_exceeding_the_guardrail_answers_a_structured_429() -> None:
    client, _, _, _ = _client()

    client.patch("/api/v1/auth/me/locale", json={"timezone_id": "Asia/Tokyo"})
    client.patch("/api/v1/auth/me/locale", json={"primary_language": "ja"})
    response = client.patch(
        "/api/v1/auth/me/locale", json={"timezone_id": "Europe/Berlin"},
    )

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["code"] == "locale_change_limit"
    assert detail["remaining_changes"] == 0
    assert detail["limit"] == LOCALE_CHANGE_LIMIT
    # The player is told *when*, not just no.
    assert datetime.fromisoformat(detail["retry_at"]) == NOW + timedelta(
        days=detail["window_days"],
    )


# ----------------------------------------------------------------------
# POST /auth/me/locale/confirm
# ----------------------------------------------------------------------


def test_confirming_clears_the_needs_confirmation_flag_on_me() -> None:
    client, _, _, _ = _client()

    before = client.get("/api/v1/auth/me")
    assert before.json()["needs_locale_confirmation"] is True

    assert client.post("/api/v1/auth/me/locale/confirm", json={}).status_code == 200

    after = client.get("/api/v1/auth/me")
    assert after.json()["needs_locale_confirmation"] is False


def test_confirmation_corrections_are_free() -> None:
    client, _, profiles, _ = _client()

    response = client.post(
        "/api/v1/auth/me/locale/confirm",
        json={
            "timezone_id": "Asia/Tokyo",
            "primary_language": "ja",
            "country_code": "JP",
            "latitude": 35.68,
            "longitude": 139.76,
            "location_label": "Tokyo, JP",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user"]["timezone_id"] == "Asia/Tokyo"
    assert payload["user"]["primary_language"] == "ja"
    assert payload["user"]["country_code"] == "JP"
    # No guardrail spend — the correction happened before any content existed.
    assert payload["remaining_changes"] == LOCALE_CHANGE_LIMIT


def test_confirming_a_second_time_cannot_move_the_locale() -> None:
    """The guardrail is only worth something if the free onboarding door
    closes behind the player."""
    client, _, profiles, _ = _client()
    assert client.post(
        "/api/v1/auth/me/locale/confirm", json={"timezone_id": "Asia/Tokyo"},
    ).status_code == 200

    response = client.post(
        "/api/v1/auth/me/locale/confirm", json={"timezone_id": "Europe/Berlin"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "locale_already_confirmed"
    # The 4xx says where to go instead of leaving the caller guessing.
    assert "PATCH /auth/me/locale" in detail["message"]


def test_dismissing_the_hint_still_works_after_confirmation() -> None:
    client, _, _, _ = _client()
    client.post("/api/v1/auth/me/locale/confirm", json={})

    response = client.post(
        "/api/v1/auth/me/locale/confirm", json={"dismiss_location_hint": True},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/api/v1/auth/me/locale", {"timezone_id": "Asia/Tokyo"}),
        ("post", "/api/v1/auth/me/locale/confirm", {"timezone_id": "Asia/Tokyo"}),
    ],
)
def test_a_write_held_by_another_replica_answers_409(
    method: str, path: str, body: dict,
) -> None:
    client, _, _, _ = _client(
        change_claim=RuntimeClaim(
            _HeldElsewhereLease(),
            prefix=LOCALE_CLAIM_PREFIX,
            owner_id="replica-a",
        ),
    )

    response = getattr(client, method)(path, json=body)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "locale_change_in_progress"


def test_confirmation_leaves_omitted_location_fields_alone() -> None:
    client, _, profiles, _ = _client()

    response = client.post(
        "/api/v1/auth/me/locale/confirm", json={"timezone_id": "Asia/Tokyo"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["location_label"] == "Taipei, TW"


# ----------------------------------------------------------------------
# GET /auth/me projection
# ----------------------------------------------------------------------


def test_me_exposes_a_pending_location_hint() -> None:
    import asyncio

    from kokoro_link.contracts.geo_location import GeoLocation

    client, service, _, _ = _client()

    async def _seed_hint() -> None:
        await service.record_location_hint(
            USER_ID,
            location=GeoLocation(
                country_code="JP",
                latitude=35.68,
                longitude=139.76,
                label="Tokyo, Japan",
                timezone_id="Asia/Tokyo",
            ),
            profile=_operator(),
        )

    asyncio.run(_seed_hint())

    payload = client.get("/api/v1/auth/me").json()

    assert payload["location_hint"]["country_code"] == "JP"
    assert payload["location_hint"]["timezone_id"] == "Asia/Tokyo"


def test_me_in_self_host_mode_carries_no_lifecycle_state() -> None:
    client, _, _, _ = _client(cloud_active=False)

    payload = client.get("/api/v1/auth/me").json()

    assert payload["needs_locale_confirmation"] is False
    assert payload["location_hint"] is None
    # The long-standing UserResponse fields are untouched.
    assert payload["id"] == USER_ID
    assert payload["timezone_id"] == "Asia/Taipei"
