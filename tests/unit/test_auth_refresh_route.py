"""``POST /auth/refresh`` — sliding renewal for a player who is still here.

Hosted sessions run on a fixed TTL (12h in the shipped hosted env), so before
this endpoint an active player was evicted mid-play on a wall-clock timer and
landed on a sign-in screen they cannot use. Renewal is deliberately *pull*:
the SPA asks only when the player has actually interacted recently, so an
abandoned tab cannot keep a session alive forever.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.auth import router as auth_router
from kokoro_link.application.services.jwt_service import JWTService
from kokoro_link.domain.entities.operator_profile import OperatorProfile


_OPERATOR = OperatorProfile(
    id="cloud:acct-hosted",
    display_name="Hosted Player",
    email="player@example.test",
    is_admin=False,
    primary_language="en",
    timezone_id="UTC",
    cloud_account_id="acct-hosted",
    cloud_tenant_id="tenant-hosted",
    cloud_tenant_tier="plus",
    auth_provider="cloud",
)


class _Strategy:
    """Resolves any non-empty token; renewal validity is the JWT service's job."""

    def __init__(self, *, resolves: bool = True) -> None:
        self._resolves = resolves
        self.verified: list[str] = []

    async def verify_token(self, token: str) -> OperatorProfile | None:
        self.verified.append(token)
        return _OPERATOR if self._resolves else None


def _client(
    *,
    auth_enabled: bool = True,
    jwt_service: JWTService | None = None,
    strategy: _Strategy | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(
            cloud=SimpleNamespace(active=auth_enabled),
            auth=SimpleNamespace(enabled=auth_enabled),
        ),
        auth_strategy=strategy or _Strategy(),
        auth_service=None,
        jwt_service=jwt_service,
        operator_profile_repository=None,
    )
    return TestClient(app)


def test_refresh_returns_a_slid_token_for_an_active_session() -> None:
    jwt_service = JWTService(secret="test-secret-key", ttl_seconds=3600)
    token = jwt_service.encode(_OPERATOR.id)
    client = _client(jwt_service=jwt_service)

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == _OPERATOR.id
    renewed = jwt_service.decode(body["token"])
    assert renewed is not None
    assert renewed["sub"] == _OPERATOR.id


def test_refresh_preserves_the_session_anchor_so_the_cap_still_applies() -> None:
    jwt_service = JWTService(
        secret="test-secret-key", ttl_seconds=3600, absolute_ttl_seconds=86400,
    )
    token = jwt_service.encode(_OPERATOR.id)
    anchor = (jwt_service.decode(token) or {})["ses"]
    client = _client(jwt_service=jwt_service)

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    renewed = jwt_service.decode(response.json()["token"])
    assert renewed is not None
    assert renewed["ses"] == anchor


def test_refresh_rejects_a_session_past_its_absolute_cap_with_401() -> None:
    """The player must go back through the Portal so the tier gate re-runs.

    The bearer still resolves to an operator — only the cap stops it, which is
    exactly the case a token-validity check alone would wave through.
    """
    import jwt as pyjwt

    from datetime import datetime, timezone

    now = int(datetime.now(timezone.utc).timestamp())
    jwt_service = JWTService(
        secret="test-secret-key", ttl_seconds=3600, absolute_ttl_seconds=7200,
    )
    over_cap = pyjwt.encode(
        {"sub": _OPERATOR.id, "iat": now - 60, "exp": now + 3600, "ses": now - 7300},
        "test-secret-key",
        algorithm="HS256",
    )
    client = _client(jwt_service=jwt_service)

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {over_cap}"},
    )

    assert response.status_code == 401


def test_refresh_requires_a_bearer_token() -> None:
    jwt_service = JWTService(secret="test-secret-key")
    client = _client(jwt_service=jwt_service)

    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_rejects_a_token_the_strategy_cannot_resolve() -> None:
    jwt_service = JWTService(secret="test-secret-key")
    client = _client(
        jwt_service=jwt_service, strategy=_Strategy(resolves=False),
    )

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {jwt_service.encode(_OPERATOR.id)}"},
    )

    assert response.status_code == 401


def test_refresh_ignores_an_access_token_query_parameter() -> None:
    """Renewal is header-only: the SSE query fallback would put a fresh
    credential into every access log along the path."""
    jwt_service = JWTService(secret="test-secret-key")
    client = _client(jwt_service=jwt_service)

    response = client.post(
        f"/api/v1/auth/refresh?access_token={jwt_service.encode(_OPERATOR.id)}",
    )

    assert response.status_code == 401


def test_refresh_is_absent_when_auth_is_disabled() -> None:
    """Self-host without auth has no session to slide."""
    client = _client(auth_enabled=False, jwt_service=JWTService(secret="test-secret"))

    assert client.post("/api/v1/auth/refresh").status_code == 404


def test_refresh_fails_closed_without_a_jwt_service() -> None:
    client = _client(jwt_service=None)

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": "Bearer whatever"},
    )

    assert response.status_code == 503
