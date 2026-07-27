"""G2 — ``GET /api/v1/geo/search`` city picker proxy (cloud mode only)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.api.routes.geo import router as geo_router
from kokoro_link.contracts.geocoding import GeocodedPlace
from kokoro_link.domain.entities.operator_profile import OperatorProfile


class _StubGeocoding:
    def __init__(self, places: list[GeocodedPlace] | None = None) -> None:
        self.places = places or []
        self.calls: list[tuple[str, str | None, int]] = []

    async def search(
        self, query: str, *, language: str | None = None, limit: int = 5,
    ) -> list[GeocodedPlace]:
        self.calls.append((query, language, limit))
        return self.places


def _operator(language: str = "zh-TW") -> OperatorProfile:
    return OperatorProfile(
        id="cloud:acct-1",
        display_name="Player",
        primary_language=language,
        timezone_id="Asia/Taipei",
        cloud_account_id="acct-1",
        cloud_tenant_id="tenant-1",
        auth_provider="cloud",
    )


def _client(
    *,
    cloud_active: bool = True,
    client_stub: _StubGeocoding | None = None,
    language: str = "zh-TW",
) -> tuple[TestClient, _StubGeocoding | None]:
    app = FastAPI()
    app.include_router(geo_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=cloud_active)),
        geocoding_client=client_stub,
    )
    app.dependency_overrides[get_current_user] = lambda: _operator(language)
    return TestClient(app), client_stub


def test_search_returns_simplified_places() -> None:
    stub = _StubGeocoding([
        GeocodedPlace(
            label="台北市, 臺灣",
            latitude=25.03,
            longitude=121.56,
            country_code="TW",
            timezone_id="Asia/Taipei",
        ),
    ])
    client, _ = _client(client_stub=stub)

    response = client.get("/api/v1/geo/search", params={"q": "台北"})

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "label": "台北市, 臺灣",
                "latitude": 25.03,
                "longitude": 121.56,
                "country_code": "TW",
                "timezone_id": "Asia/Taipei",
            },
        ],
    }


def test_search_language_follows_the_operator_content_language() -> None:
    stub = _StubGeocoding()
    client, _ = _client(client_stub=stub, language="ja")

    client.get("/api/v1/geo/search", params={"q": "東京"})

    assert stub.calls == [("東京", "ja", 5)]


def test_a_blank_query_is_forwarded_and_answers_empty() -> None:
    """The adapter owns the min-length rule; the route stays a thin proxy."""
    stub = _StubGeocoding()
    client, _ = _client(client_stub=stub)

    response = client.get("/api/v1/geo/search")

    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_an_unwired_client_degrades_to_an_empty_list() -> None:
    client, _ = _client(client_stub=None)

    response = client.get("/api/v1/geo/search", params={"q": "台北"})

    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_route_is_hidden_in_self_host_mode() -> None:
    stub = _StubGeocoding()
    client, _ = _client(cloud_active=False, client_stub=stub)

    response = client.get("/api/v1/geo/search", params={"q": "台北"})

    assert response.status_code == 404
    assert stub.calls == []
