"""PP1 — ``current_status`` retirement, self-host compat seam.

An already-deployed self-hosted frontend can still send ``current_status``
on every profile save (browser caches an old bundle indefinitely; there is
no forced-update mechanism). The field is retired server-side — dropped
from the entity, the row, and the prompt — but the API must not 400 on it:
that would turn an inert legacy field into an outage for anyone who hasn't
refreshed their tab. This pins the accept-and-ignore contract end to end.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.api.routes.operator import router as operator_router
from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)

USER_ID = "user-1"


def _build() -> TestClient:
    profiles = InMemoryOperatorProfileRepository()
    operator = OperatorProfile(id=USER_ID, display_name="艾力")
    profiles._profiles[operator.id] = operator  # noqa: SLF001 - test seam

    app = FastAPI()
    app.include_router(operator_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=False)),
        operator_profile_service=OperatorProfileService(repository=profiles),
        player_locale_service=None,
    )
    app.dependency_overrides[get_current_user] = lambda: operator
    return TestClient(app)


def test_legacy_current_status_payload_is_silently_ignored() -> None:
    client = _build()

    response = client.put(
        "/api/v1/operator/profile",
        json={"display_name": "阿力", "current_status": "今天出差"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "阿力"
    assert "current_status" not in body
    assert "current_status_set_at" not in body


def test_current_status_alone_still_saves_no_trace_of_it() -> None:
    """A payload that carries *only* the dead field (no other change) must
    still round-trip as a no-op 200 rather than error, and the stored
    profile must carry no trace of the value that was sent."""
    client = _build()

    response = client.put(
        "/api/v1/operator/profile",
        json={"current_status": "在醫院陪家人"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "艾力"
    assert "current_status" not in body

    followup = client.get("/api/v1/operator/profile")
    assert followup.status_code == 200
    assert "current_status" not in followup.json()
