from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.application.exceptions import DemoSessionUnavailable
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


class _DemoAuthStrategy:
    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []
        self.last_seed: CloudProfileSeed | None = None

    async def login_with_demo_session(
        self,
        *,
        provider: str,
        authorization_code: str,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        source_ip: str | None = None,
        device_id: str | None = None,
        profile_seed: CloudProfileSeed | None = None,
    ) -> tuple[OperatorProfile, str]:
        self.calls.append({
            "provider": provider,
            "authorization_code": authorization_code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "source_ip": source_ip,
            "device_id": device_id,
        })
        self.last_seed = profile_seed
        return (
            OperatorProfile(
                id="cloud-demo-user",
                display_name="Cloud Demo User",
                email="demo@example.test",
                is_admin=False,
                primary_language="en",
                timezone_id="UTC",
                cloud_account_id="acct-demo",
                cloud_tenant_id="tenant-demo",
                cloud_tenant_tier="demo",
                auth_provider="cloud",
            ),
            "core-demo-token",
        )


def _client(
    *,
    cloud_active: bool,
    strategy: _DemoAuthStrategy | None = None,
    geo_provider: _StubGeoProvider | None = None,
) -> tuple[TestClient, _DemoAuthStrategy]:
    strategy = strategy or _DemoAuthStrategy()
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


def test_demo_session_route_exchanges_code_in_cloud_mode() -> None:
    client, strategy = _client(cloud_active=True)

    response = client.post(
        "/api/v1/auth/demo/session",
        headers={
            "x-real-ip": "198.51.100.44",
            "x-forwarded-for": "203.0.113.99",
            "x-yuralume-demo-device": "device-1",
        },
        json={
            "provider": "discord",
            "authorization_code": "oauth-code",
            "redirect_uri": "https://app.example/demo/oauth/discord/callback",
            "code_verifier": "pkce-verifier",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "token": "core-demo-token",
        "user": {
            "id": "cloud-demo-user",
            "display_name": "Cloud Demo User",
            "display_name_is_placeholder": False,
            "email": "demo@example.test",
            "is_admin": False,
            "primary_language": "en",
            "timezone_id": "UTC",
            "country_code": None,
            "latitude": None,
            "longitude": None,
            "location_label": None,
        },
    }
    assert strategy.calls == [{
        "provider": "discord",
        "authorization_code": "oauth-code",
        "redirect_uri": "https://app.example/demo/oauth/discord/callback",
        "code_verifier": "pkce-verifier",
        "source_ip": "198.51.100.44",
        "device_id": "device-1",
    }]


def test_demo_session_route_passes_geo_seed_to_strategy() -> None:
    geo = _StubGeoProvider(GeoLocation(
        country_code="JP",
        latitude=35.68,
        longitude=139.69,
        label="Tokyo, JP",
        timezone_id="Asia/Tokyo",
    ))
    client, strategy = _client(cloud_active=True, geo_provider=geo)

    response = client.post(
        "/api/v1/auth/demo/session",
        json={"provider": "discord", "authorization_code": "oauth-code"},
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


def test_demo_session_route_passes_empty_seed_without_geo_provider() -> None:
    client, strategy = _client(cloud_active=True)

    response = client.post(
        "/api/v1/auth/demo/session",
        json={"provider": "discord", "authorization_code": "oauth-code"},
    )

    assert response.status_code == 200
    assert strategy.last_seed == CloudProfileSeed()


def test_demo_session_route_is_hidden_in_self_host_mode() -> None:
    client, strategy = _client(cloud_active=False)

    response = client.post(
        "/api/v1/auth/demo/session",
        json={
            "provider": "discord",
            "authorization_code": "oauth-code",
        },
    )

    assert response.status_code == 404
    assert strategy.calls == []


def test_demo_session_route_returns_structured_demo_busy_error() -> None:
    class BusyStrategy(_DemoAuthStrategy):
        async def login_with_demo_session(
            self,
            *,
            provider: str,
            authorization_code: str,
            redirect_uri: str | None = None,
            code_verifier: str | None = None,
            source_ip: str | None = None,
            device_id: str | None = None,
            profile_seed: CloudProfileSeed | None = None,
        ) -> tuple[OperatorProfile, str]:
            raise DemoSessionUnavailable(
                status_code=503,
                code="demo_busy",
                message="demo slots are full",
                retryable=True,
            )

    client, _ = _client(cloud_active=True, strategy=BusyStrategy())

    response = client.post(
        "/api/v1/auth/demo/session",
        json={
            "provider": "discord",
            "authorization_code": "oauth-code",
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": {
                "code": "demo_busy",
                "message": "demo slots are full",
                "retryable": True,
            },
        },
    }
