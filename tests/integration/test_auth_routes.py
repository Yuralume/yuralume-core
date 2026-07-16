"""End-to-end auth router tests — disabled mode + enabled mode flows.

Builds a real FastAPI app via ``create_app`` so the dependency wiring,
exception handler, and middleware are exercised. Each test mutates
``KOKORO_AUTH_ENABLED`` via env so we observe the two operating modes
without forking the test process.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.contracts.cloud_auth import CloudAccountIdentity
from kokoro_link.contracts.geo_location import GeoLocation
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.operator_profile import OperatorProfile


@pytest.fixture
def app_disabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """auth_enabled=false: no token required, every request runs as
    default user.

    Force ``KOKORO_DATABASE_URL=""`` so the container uses the
    in-memory repos (otherwise the dev ``.env`` would attach to the
    real Postgres dev DB and inherit live state from earlier sessions,
    making this test depend on dev environment cleanliness)."""
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.delenv("KOKORO_JWT_SECRET", raising=False)
    # Pin the deploy-time locale so the synthesised default operator is
    # deterministic regardless of the dev machine's .env (which may set a
    # non-UTC USER_TIMEZONE). The dedicated locale-env test below overrides
    # these explicitly.
    monkeypatch.setenv("USER_TIMEZONE", "UTC")
    monkeypatch.setenv("USER_PRIMARY_LANGUAGE", "zh-TW")
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def app_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """auth_enabled=true: bearer required, /setup → /login → /me flow."""
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "test-jwt-secret-that-is-at-least-32-bytes-long-x",
    )
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def app_cloud(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """cloud mode: auth surface is enabled and local write paths lock."""
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "cloud-mode-test-secret-at-least-32-bytes",
    )
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YURALUME_CLOUD_USER_SERVICE_URL", "https://users.example")
    monkeypatch.setenv("YURALUME_CLOUD_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_TOKEN", "deploy-secret")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_ID", "hosted-primary")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "yuralume-gateway")
    monkeypatch.setenv("YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", "core-kid|core|yuralume-user|demo-session:release,introspection:session,runtime:read|core-secret")
    app = create_app()
    with TestClient(app) as client:
        yield client


# ----------------------------------------------------------------------
# Disabled mode: auth bypassed
# ----------------------------------------------------------------------


def test_config_reports_disabled(app_disabled: TestClient) -> None:
    res = app_disabled.get("/api/v1/auth/config")
    assert res.status_code == 200
    payload = res.json()
    assert payload["auth_enabled"] is False
    # Default user in disabled mode never has a password set up.
    assert payload["needs_setup"] is True


def test_disabled_mode_lets_characters_endpoint_through_without_token(
    app_disabled: TestClient,
) -> None:
    res = app_disabled.get("/api/v1/characters")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_disabled_mode_me_returns_default_user(
    app_disabled: TestClient,
) -> None:
    res = app_disabled.get("/api/v1/auth/me")
    assert res.status_code == 200
    payload = res.json()
    # In disabled mode we synthesise a default OperatorProfile so the
    # route always has a user; the in-memory test container has no
    # repo so the synthetic default surfaces (display_name=操作者).
    assert payload["id"] == "default"
    assert payload["display_name"] == "操作者"
    assert payload["timezone_id"] == "UTC"


def test_disabled_mode_me_honours_default_locale_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USER_PRIMARY_LANGUAGE / USER_TIMEZONE drive the default operator's
    language + timezone end to end, so a single-user self-host comes up in
    the operator's language and local time (SPA chrome reads these via
    /auth/me → applyPrimaryLanguage / applyUserTimezone)."""
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv("USER_PRIMARY_LANGUAGE", "en-US")
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Taipei")
    monkeypatch.delenv("KOKORO_USER_PRIMARY_LANGUAGE", raising=False)
    monkeypatch.delenv("KOKORO_USER_TIMEZONE", raising=False)
    app = create_app()
    with TestClient(app) as client:
        res = client.get("/api/v1/auth/me")
    assert res.status_code == 200
    payload = res.json()
    assert payload["primary_language"] == "en-US"
    assert payload["timezone_id"] == "Asia/Taipei"


def test_disabled_mode_setup_without_seed_returns_503(
    app_disabled: TestClient,
) -> None:
    """In-memory test container has no pre-seeded default user (real
    deployments get the row from alembic migration ct5y7z00070). The
    setup endpoint correctly surfaces this as 503 'default user row
    missing — run alembic upgrade head' rather than silently creating
    a new admin out of thin air."""
    res = app_disabled.post(
        "/api/v1/auth/setup",
        json={"email": "me@example.com", "password": "hunter2"},
    )
    assert res.status_code == 503


def test_enabled_mode_full_setup_login_me_flow(
    app_enabled: TestClient,
) -> None:
    """The end-to-end happy path after seed:
    setup → login → /auth/me with token → /characters with token.

    Skipped when the in-memory container can't satisfy setup (no
    default row). When wired to real Postgres + migration this becomes
    the smoke test we run after every deploy."""
    setup = app_enabled.post(
        "/api/v1/auth/setup",
        json={"email": "admin@example.com", "password": "hunter2"},
    )
    if setup.status_code == 503:
        pytest.skip("in-memory container has no default user row")
    assert setup.status_code == 201
    token = setup.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = app_enabled.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"
    assert me.json()["timezone_id"] == "UTC"

    chars = app_enabled.get("/api/v1/characters", headers=headers)
    assert chars.status_code == 200
    assert isinstance(chars.json(), list)

    # Login again with the same credentials works.
    login = app_enabled.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "hunter2"},
    )
    assert login.status_code == 200
    assert login.json()["token"]


# ----------------------------------------------------------------------
# Enabled mode: bearer mandatory
# ----------------------------------------------------------------------


def test_enabled_mode_characters_endpoint_rejects_anonymous(
    app_enabled: TestClient,
) -> None:
    res = app_enabled.get("/api/v1/characters")
    assert res.status_code == 401
    assert res.headers.get("www-authenticate") == "Bearer"


def test_enabled_mode_me_requires_bearer(
    app_enabled: TestClient,
) -> None:
    res = app_enabled.get("/api/v1/auth/me")
    assert res.status_code == 401


def test_enabled_mode_config_still_public(
    app_enabled: TestClient,
) -> None:
    """Front-end startup probe must work without a token even when
    auth is enabled — otherwise the UI can't decide whether to route
    to /login."""
    res = app_enabled.get("/api/v1/auth/config")
    assert res.status_code == 200
    payload = res.json()
    assert payload["auth_enabled"] is True


def test_cloud_mode_config_reports_cloud_mode(app_cloud: TestClient) -> None:
    res = app_cloud.get("/api/v1/auth/config")

    assert res.status_code == 200
    payload = res.json()
    assert payload["auth_enabled"] is True
    assert payload["needs_setup"] is False
    assert payload["mode"] == "cloud"


def test_cloud_mode_locks_local_setup(app_cloud: TestClient) -> None:
    res = app_cloud.post(
        "/api/v1/auth/setup",
        json={"email": "admin@example.com", "password": "hunter2"},
    )

    assert res.status_code == 403
    assert res.json()["detail"] == "self-host auth management is disabled in cloud mode"


def test_cloud_mode_locks_local_user_create(app_cloud: TestClient) -> None:
    token = _seed_auth_user(
        app_cloud,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
        is_admin=True,
        auth_provider="cloud",
        cloud_account_id="acct_alice",
        cloud_tenant_id="tenant_1",
    )

    res = app_cloud.post(
        "/api/v1/auth/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "email": "bob@example.com",
            "password": "hunter2",
            "display_name": "Bob",
        },
    )

    assert res.status_code == 403
    assert res.json()["detail"] == "self-host auth management is disabled in cloud mode"


def test_cloud_mode_locks_user_list(app_cloud: TestClient) -> None:
    # Listing operator projections is also self-host auth management — a cloud
    # admin must not enumerate every player's projection through the core.
    token = _seed_auth_user(
        app_cloud,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
        is_admin=True,
        auth_provider="cloud",
        cloud_account_id="acct_alice",
        cloud_tenant_id="tenant_1",
    )

    res = app_cloud.get(
        "/api/v1/auth/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 403
    assert res.json()["detail"] == "self-host auth management is disabled in cloud mode"


def test_cloud_mode_federated_login_projects_operator(app_cloud: TestClient) -> None:
    app_cloud.app.state.container.auth_strategy._user_service = (  # noqa: SLF001
        _StubCloudUserService(CloudAccountIdentity(
            account_id="acct_1",
            tenant_id="tenant_1",
            role="member",
            status="active",
            email="player@example.com",
            display_name="Player",
            primary_language="en-US",
            timezone_id="Asia/Taipei",
        ))
    )

    login = app_cloud.post(
        "/api/v1/auth/login",
        json={"email": "player@example.com", "password": "secret"},
    )

    assert login.status_code == 200
    payload = login.json()
    assert payload["token"]
    assert payload["user"]["id"] == "cloud:acct_1"
    assert payload["user"]["email"] == "player@example.com"
    assert payload["user"]["is_admin"] is False
    headers = {"Authorization": f"Bearer {payload['token']}"}

    me = app_cloud.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == "cloud:acct_1"


def test_enabled_mode_setup_status_public(
    app_enabled: TestClient,
) -> None:
    res = app_enabled.get("/api/v1/auth/setup-status")
    assert res.status_code == 200
    assert "needs_setup" in res.json()


def test_enabled_mode_login_with_garbage_credentials_returns_401(
    app_enabled: TestClient,
) -> None:
    res = app_enabled.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": "anything"},
    )
    assert res.status_code == 401


def test_enabled_mode_invalid_token_rejected(
    app_enabled: TestClient,
) -> None:
    res = app_enabled.get(
        "/api/v1/characters",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert res.status_code == 401


# ----------------------------------------------------------------------
# primary_language — Phase 1a of FRONTEND_I18N_PLAN
# ----------------------------------------------------------------------


def test_setup_returns_primary_language_default(
    app_enabled: TestClient,
) -> None:
    """Setup payload without ``primary_language`` → defaults to zh-TW.
    The /me response should echo it back so the frontend can use it as
    a fallback for the UI locale switcher."""
    setup = app_enabled.post(
        "/api/v1/auth/setup",
        json={"email": "admin@example.com", "password": "hunter2"},
    )
    if setup.status_code == 503:
        pytest.skip("in-memory container has no default user row")
    assert setup.status_code == 201
    assert setup.json()["user"]["primary_language"] == "zh-TW"
    token = setup.json()["token"]
    me = app_enabled.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me.status_code == 200
    assert me.json()["primary_language"] == "zh-TW"


def test_setup_persists_explicit_primary_language(
    app_enabled: TestClient,
) -> None:
    setup = app_enabled.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": "hunter2",
            "primary_language": "en-US",
        },
    )
    if setup.status_code == 503:
        pytest.skip("in-memory container has no default user row")
    assert setup.status_code == 201
    assert setup.json()["user"]["primary_language"] == "en-US"


def test_setup_accepts_japanese_primary_language(
    app_enabled: TestClient,
) -> None:
    _seed_default_operator(app_enabled)

    setup = app_enabled.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": "hunter2",
            "primary_language": "ja-jp",
        },
    )

    assert setup.status_code == 201
    assert setup.json()["user"]["primary_language"] == "ja-JP"
    me = app_enabled.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {setup.json()['token']}"},
    )
    assert me.status_code == 200
    assert me.json()["primary_language"] == "ja-JP"


def test_setup_normalises_primary_language_casing(
    app_enabled: TestClient,
) -> None:
    setup = app_enabled.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": "hunter2",
            "primary_language": "en-us",
        },
    )
    if setup.status_code == 503:
        pytest.skip("in-memory container has no default user row")
    assert setup.status_code == 201
    assert setup.json()["user"]["primary_language"] == "en-US"


def test_setup_rejects_malformed_primary_language(
    app_enabled: TestClient,
) -> None:
    """Structurally broken tag → 400 (mapped from InvalidCredentials)."""
    res = app_enabled.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": "hunter2",
            "primary_language": "1nv4l1d",  # numerics in language subtag
        },
    )
    # 400 (InvalidCredentials → 400) or 422 (pydantic min_length); both
    # are acceptable rejection paths — what we don't want is 201.
    assert res.status_code in (400, 422)


def test_admin_create_user_accepts_japanese_primary_language(
    app_enabled: TestClient,
) -> None:
    token = _seed_auth_user(
        app_enabled,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
        is_admin=True,
    )

    created = app_enabled.post(
        "/api/v1/auth/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "email": "bob@example.com",
            "password": "hunter2",
            "display_name": "Bob",
            "primary_language": "ja-jp",
        },
    )

    assert created.status_code == 201
    assert created.json()["primary_language"] == "ja-JP"


def test_no_patch_endpoint_for_primary_language(
    app_enabled: TestClient,
) -> None:
    """The plan forbids letting users mutate primary_language after
    registration. A direct PATCH must 404 / 405 — not 200."""
    setup = app_enabled.post(
        "/api/v1/auth/setup",
        json={"email": "admin@example.com", "password": "hunter2"},
    )
    if setup.status_code == 503:
        pytest.skip("in-memory container has no default user row")
    token = setup.json()["token"]
    user_id = setup.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {token}"}
    res = app_enabled.patch(
        f"/api/v1/auth/users/{user_id}/primary-language",
        headers=headers,
        json={"primary_language": "en-US"},
    )
    assert res.status_code in (404, 405)


# ----------------------------------------------------------------------
# Password changes
# ----------------------------------------------------------------------


def _seed_auth_user(
    app_enabled: TestClient,
    *,
    user_id: str,
    email: str,
    password: str,
    is_admin: bool = False,
    auth_provider: str = "local",
    cloud_account_id: str | None = None,
    cloud_tenant_id: str | None = None,
    country_code: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    location_label: str | None = None,
) -> str:
    container = app_enabled.app.state.container
    user = OperatorProfile(
        id=user_id,
        display_name=user_id.title(),
        email=email,
        password_hash=container.password_hasher.hash(password),
        is_admin=is_admin,
        auth_provider=auth_provider,
        cloud_account_id=cloud_account_id,
        cloud_tenant_id=cloud_tenant_id,
        country_code=country_code,
        latitude=latitude,
        longitude=longitude,
        location_label=location_label,
    )

    async def seed() -> None:
        await container.operator_profile_repository.save(user)

    import asyncio
    asyncio.run(seed())
    return container.jwt_service.encode(user_id)


class _FakeGeoLocationProvider:
    def __init__(self, location: GeoLocation | None) -> None:
        self.location = location
        self.seen_ips: list[str] = []

    async def locate(self, ip: str) -> GeoLocation | None:
        self.seen_ips.append(ip)
        return self.location


class _StubCloudUserService:
    def __init__(self, identity: CloudAccountIdentity) -> None:
        self.identity = identity

    async def login(self, *, email: str, password: str) -> CloudAccountIdentity:
        return self.identity


def _seed_default_operator(app_enabled: TestClient) -> None:
    container = app_enabled.app.state.container

    async def seed() -> None:
        await container.operator_profile_repository.save(OperatorProfile(
            id=DEFAULT_OPERATOR_ID,
            display_name="操作者",
            email=None,
            password_hash=None,
            is_admin=True,
        ))

    import asyncio
    asyncio.run(seed())


def test_setup_omitted_location_does_not_use_request_ip_geo_location(
    app_enabled: TestClient,
) -> None:
    _seed_default_operator(app_enabled)
    geo = _FakeGeoLocationProvider(GeoLocation(
        country_code="US",
        latitude=37.7749,
        longitude=-122.4194,
        label="San Francisco, US",
    ))
    app_enabled.app.state.container.geo_location_provider = geo

    setup = app_enabled.post(
        "/api/v1/auth/setup",
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.4"},
        json={"email": "admin@example.com", "password": "hunter2"},
    )

    assert setup.status_code == 201
    user = setup.json()["user"]
    assert geo.seen_ips == []
    assert user["country_code"] is None
    assert user["latitude"] is None
    assert user["longitude"] is None
    assert user["location_label"] is None


def test_login_seeds_empty_location_from_forwarded_ip(
    app_enabled: TestClient,
) -> None:
    _seed_auth_user(
        app_enabled,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
    )
    geo = _FakeGeoLocationProvider(GeoLocation(
        country_code="US",
        latitude=37.7749,
        longitude=-122.4194,
        label="San Francisco, US",
    ))
    app_enabled.app.state.container.geo_location_provider = geo

    login = app_enabled.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.4"},
        json={"email": "alice@example.com", "password": "oldpass"},
    )

    assert login.status_code == 200
    user = login.json()["user"]
    assert geo.seen_ips == ["203.0.113.10"]
    assert user["country_code"] == "US"
    assert user["latitude"] == 37.7749
    assert user["longitude"] == -122.4194
    assert user["location_label"] == "San Francisco, US"

    me = app_enabled.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert me.status_code == 200
    assert me.json()["location_label"] == "San Francisco, US"


def test_login_logs_ip_header_candidates(
    app_enabled: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_auth_user(
        app_enabled,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
    )
    geo = _FakeGeoLocationProvider(GeoLocation(
        country_code="US",
        latitude=37.7749,
        longitude=-122.4194,
        label="San Francisco, US",
    ))
    app_enabled.app.state.container.geo_location_provider = geo
    caplog.set_level(logging.INFO, logger="kokoro_link.api.routes.auth")

    login = app_enabled.post(
        "/api/v1/auth/login",
        headers={
            "Forwarded": "for=198.51.100.44;proto=https",
            "X-Forwarded-For": "203.0.113.10, 10.0.0.4",
            "X-Real-IP": "10.0.0.4",
            "CF-Connecting-IP": "198.51.100.55",
        },
        json={"email": "alice@example.com", "password": "oldpass"},
    )

    assert login.status_code == 200
    diagnostics = [
        record.getMessage()
        for record in caplog.records
        if "Login IP diagnostics" in record.getMessage()
    ]
    assert len(diagnostics) == 1
    assert "user_id=alice" in diagnostics[0]
    assert "has_existing_location=False" in diagnostics[0]
    assert "selected_client_ip=203.0.113.10" in diagnostics[0]
    assert "'forwarded': 'for=198.51.100.44;proto=https'" in diagnostics[0]
    assert "'x-forwarded-for': '203.0.113.10, 10.0.0.4'" in diagnostics[0]
    assert "'x-real-ip': '10.0.0.4'" in diagnostics[0]
    assert "'cf-connecting-ip': '198.51.100.55'" in diagnostics[0]
    assert "request.client.host" in diagnostics[0]
    assert any(
        "Login GeoIP result: user_id=alice selected_client_ip=203.0.113.10 "
        "result={'country_code': 'US', 'latitude': 37.7749, "
        "'longitude': -122.4194, 'label': 'San Francisco, US'}"
        in record.getMessage()
        for record in caplog.records
    )
    assert any(
        "Login GeoIP seed fields: user_id=alice country_code=US "
        "latitude=37.7749 longitude=-122.4194 "
        "location_label=San Francisco, US"
        in record.getMessage()
        for record in caplog.records
    )


def test_login_does_not_override_existing_location(
    app_enabled: TestClient,
) -> None:
    _seed_auth_user(
        app_enabled,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
        country_code="JP",
        latitude=35.6762,
        longitude=139.6503,
        location_label="Tokyo, JP",
    )
    geo = _FakeGeoLocationProvider(GeoLocation(
        country_code="US",
        latitude=37.7749,
        longitude=-122.4194,
        label="San Francisco, US",
    ))
    app_enabled.app.state.container.geo_location_provider = geo

    login = app_enabled.post(
        "/api/v1/auth/login",
        headers={"X-Real-IP": "203.0.113.10"},
        json={"email": "alice@example.com", "password": "oldpass"},
    )

    assert login.status_code == 200
    user = login.json()["user"]
    assert geo.seen_ips == []
    assert user["country_code"] == "JP"
    assert user["latitude"] == 35.6762
    assert user["longitude"] == 139.6503
    assert user["location_label"] == "Tokyo, JP"


def test_setup_payload_location_overrides_ip_geo_location(
    app_enabled: TestClient,
) -> None:
    _seed_default_operator(app_enabled)
    geo = _FakeGeoLocationProvider(GeoLocation(
        country_code="US",
        latitude=37.7749,
        longitude=-122.4194,
        label="San Francisco, US",
    ))
    app_enabled.app.state.container.geo_location_provider = geo

    setup = app_enabled.post(
        "/api/v1/auth/setup",
        headers={"X-Real-IP": "203.0.113.10"},
        json={
            "email": "admin@example.com",
            "password": "hunter2",
            "country_code": "JP",
            "latitude": 35.6762,
            "longitude": 139.6503,
            "location_label": "Tokyo, JP",
        },
    )

    assert setup.status_code == 201
    user = setup.json()["user"]
    assert geo.seen_ips == []
    assert user["country_code"] == "JP"
    assert user["latitude"] == 35.6762
    assert user["longitude"] == 139.6503
    assert user["location_label"] == "Tokyo, JP"


def test_current_user_can_change_own_password_with_current_password(
    app_enabled: TestClient,
) -> None:
    token = _seed_auth_user(
        app_enabled,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
    )
    headers = {"Authorization": f"Bearer {token}"}

    changed = app_enabled.post(
        "/api/v1/auth/me/password",
        headers=headers,
        json={
            "current_password": "oldpass",
            "new_password": "newpass",
        },
    )

    assert changed.status_code == 200
    assert changed.json()["email"] == "alice@example.com"

    old_login = app_enabled.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "oldpass"},
    )
    assert old_login.status_code == 401

    new_login = app_enabled.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "newpass"},
    )
    assert new_login.status_code == 200
    assert new_login.json()["token"]


def test_current_user_password_change_rejects_wrong_current_password(
    app_enabled: TestClient,
) -> None:
    token = _seed_auth_user(
        app_enabled,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
    )

    changed = app_enabled.post(
        "/api/v1/auth/me/password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": "wrong",
            "new_password": "newpass",
        },
    )

    assert changed.status_code == 400

    old_login = app_enabled.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "oldpass"},
    )
    assert old_login.status_code == 200


def test_user_password_reset_endpoint_is_admin_only(
    app_enabled: TestClient,
) -> None:
    token = _seed_auth_user(
        app_enabled,
        user_id="alice",
        email="alice@example.com",
        password="oldpass",
    )

    response = app_enabled.post(
        "/api/v1/auth/users/alice/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"new_password": "newpass"},
    )

    assert response.status_code == 403
