from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.application.exceptions import (
    InvalidCredentials,
    SetupNotAllowed,
)
from kokoro_link.api.routes.auth import router as auth_router
from kokoro_link.contracts.cloud_auth import CloudProfileSeed
from kokoro_link.contracts.geo_location import GeoLocation
from kokoro_link.domain.entities.operator_profile import OperatorProfile


class _StubGeoProvider:
    def __init__(self, location: GeoLocation | None) -> None:
        self._location = location
        self.located_ips: list[str] = []

    async def locate(self, ip: str) -> GeoLocation | None:
        self.located_ips.append(ip)
        return self._location


class _CloudPlayStrategy:
    def __init__(self) -> None:
        self.codes: list[str] = []
        self.last_seed: CloudProfileSeed | None = None

    async def login_with_cloud_play_code(
        self,
        *,
        code: str,
        profile_seed: CloudProfileSeed | None = None,
    ) -> tuple[OperatorProfile, str]:
        self.codes.append(code)
        self.last_seed = profile_seed
        return (
            OperatorProfile(
                id="cloud:acct-hosted",
                display_name="Hosted Player",
                email="player@example.test",
                is_admin=False,
                primary_language="en",
                timezone_id="UTC",
                cloud_account_id="acct-hosted",
                cloud_tenant_id="tenant-hosted",
                cloud_tenant_tier="standard",
                auth_provider="cloud",
            ),
            "core-hosted-token",
        )


def _client(
    *,
    cloud_active: bool,
    strategy: _CloudPlayStrategy | None = None,
    geo_provider: _StubGeoProvider | None = None,
) -> tuple[TestClient, _CloudPlayStrategy]:
    strategy = strategy or _CloudPlayStrategy()
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(
            cloud=SimpleNamespace(active=cloud_active),
        ),
        auth_strategy=strategy,
        geo_location_provider=geo_provider,
        operator_profile_repository=None,
    )
    return TestClient(app), strategy


def test_cloud_session_route_exchanges_code_in_cloud_mode() -> None:
    client, strategy = _client(cloud_active=True)

    response = client.post(
        "/api/v1/auth/cloud/session",
        json={"code": "yhp_entry_code"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "token": "core-hosted-token",
        "user": {
            "id": "cloud:acct-hosted",
            "display_name": "Hosted Player",
            "display_name_is_placeholder": False,
            "email": "player@example.test",
            "is_admin": False,
            "primary_language": "en",
            "timezone_id": "UTC",
            "country_code": None,
            "latitude": None,
            "longitude": None,
            "location_label": None,
        },
    }
    assert strategy.codes == ["yhp_entry_code"]


def test_cloud_session_route_passes_geo_seed_to_strategy() -> None:
    geo = _StubGeoProvider(GeoLocation(
        country_code="JP",
        latitude=35.68,
        longitude=139.69,
        label="Tokyo, JP",
        timezone_id="Asia/Tokyo",
    ))
    client, strategy = _client(cloud_active=True, geo_provider=geo)

    response = client.post(
        "/api/v1/auth/cloud/session",
        json={"code": "yhp_entry_code"},
        headers={"x-forwarded-for": "203.0.113.7"},
    )

    assert response.status_code == 200
    assert geo.located_ips == ["203.0.113.7"]
    assert strategy.last_seed == CloudProfileSeed(
        timezone_id="Asia/Tokyo",
        country_code="JP",
        latitude=35.68,
        longitude=139.69,
        location_label="Tokyo, JP",
    )


def test_cloud_session_route_passes_empty_seed_without_geo_provider() -> None:
    client, strategy = _client(cloud_active=True)

    response = client.post(
        "/api/v1/auth/cloud/session",
        json={"code": "yhp_entry_code"},
    )

    assert response.status_code == 200
    assert strategy.last_seed == CloudProfileSeed()


def test_cloud_session_route_is_hidden_in_self_host_mode() -> None:
    client, strategy = _client(cloud_active=False)

    response = client.post(
        "/api/v1/auth/cloud/session",
        json={"code": "yhp_entry_code"},
    )

    assert response.status_code == 404
    assert strategy.codes == []


def test_cloud_session_route_translates_invalid_code_to_401() -> None:
    class RejectStrategy(_CloudPlayStrategy):
        async def login_with_cloud_play_code(
            self,
            *,
            code: str,
            profile_seed: CloudProfileSeed | None = None,
        ) -> tuple[OperatorProfile, str]:
            raise InvalidCredentials()

    client, _ = _client(cloud_active=True, strategy=RejectStrategy())

    response = client.post(
        "/api/v1/auth/cloud/session",
        json={"code": "yhp_bad"},
    )

    assert response.status_code == 401


def test_cloud_session_route_translates_upstream_error_to_503() -> None:
    class UpstreamStrategy(_CloudPlayStrategy):
        async def login_with_cloud_play_code(
            self,
            *,
            code: str,
            profile_seed: CloudProfileSeed | None = None,
        ) -> tuple[OperatorProfile, str]:
            raise SetupNotAllowed("cloud user service unavailable")

    client, _ = _client(cloud_active=True, strategy=UpstreamStrategy())

    response = client.post(
        "/api/v1/auth/cloud/session",
        json={"code": "yhp_entry_code"},
    )

    assert response.status_code == 503
