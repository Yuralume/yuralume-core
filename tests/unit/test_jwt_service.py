"""JWT encode/decode + expiration + tamper resistance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.jwt_service import JWTService


def test_encode_decode_round_trip() -> None:
    svc = JWTService(secret="test-secret-key")
    token = svc.encode("user-42")
    payload = svc.decode(token)
    assert payload is not None
    assert payload["sub"] == "user-42"


def test_user_id_from_round_trip() -> None:
    svc = JWTService(secret="test-secret-key")
    token = svc.encode("user-7")
    assert svc.user_id_from(token) == "user-7"


def test_decode_returns_none_for_garbage() -> None:
    svc = JWTService(secret="test-secret-key")
    assert svc.decode("not-a-token") is None
    assert svc.decode("") is None


def test_decode_returns_none_for_different_secret() -> None:
    a = JWTService(secret="secret-a")
    b = JWTService(secret="secret-b")
    token = a.encode("user-1")
    assert b.decode(token) is None


def test_decode_returns_none_for_expired_token() -> None:
    """Encode a token whose iat/exp are already in the past — PyJWT's
    real-clock decode must reject it.

    We can only inject our clock into ``encode`` (PyJWT's ``decode``
    uses ``time.time()`` internally, no hook to override), so we
    simulate expiry by issuing with a past timestamp."""
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)

    svc = JWTService(secret="test", ttl_seconds=60, clock=lambda: past)
    token = svc.encode("user-1")
    # Real "now" is years past the exp claim → must be rejected.
    assert svc.decode(token) is None


def test_encode_rejects_empty_user_id() -> None:
    svc = JWTService(secret="test")
    with pytest.raises(ValueError):
        svc.encode("")
    with pytest.raises(ValueError):
        svc.encode("   ")


def test_constructor_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        JWTService(secret="")
    with pytest.raises(ValueError):
        JWTService(secret="   ")


def test_ttl_floor_60_seconds() -> None:
    """TTL clamped to a minimum so a fat-fingered config can't issue
    instantly-expired tokens."""
    svc = JWTService(secret="test", ttl_seconds=1)
    token = svc.encode("user-1")
    assert svc.decode(token) is not None
