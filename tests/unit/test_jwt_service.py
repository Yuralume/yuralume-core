"""JWT encode/decode + expiration + tamper resistance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
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


# ----------------------------------------------------------------------
# Sliding renewal (active players must not be evicted mid-session)
# ----------------------------------------------------------------------


def _seconds_ago(seconds: int) -> int:
    return int(datetime.now(timezone.utc).timestamp()) - seconds


def test_encode_stamps_the_session_anchor_on_first_issue() -> None:
    svc = JWTService(secret="test")
    payload = svc.decode(svc.encode("user-1"))
    assert payload is not None
    assert payload["ses"] == payload["iat"]


def test_renew_slides_expiry_forward_and_preserves_the_session_anchor() -> None:
    # A movable clock: renewal only shows up as a later expiry if time
    # actually advanced between the two issues. It runs *behind* real time so
    # neither token carries a future ``iat``, which PyJWT's decode rejects.
    real_now = datetime.now(timezone.utc)
    current = real_now - timedelta(seconds=30)
    svc = JWTService(secret="test", ttl_seconds=3600, clock=lambda: current)
    started_at = _seconds_ago(1800)
    original = svc.encode("user-1", session_started_at=started_at)
    original_payload = svc.decode(original)

    current = real_now
    renewed = svc.renew(original)

    assert renewed is not None
    payload = svc.decode(renewed)
    assert payload is not None
    assert original_payload is not None
    assert payload["sub"] == "user-1"
    # The anchor is what the absolute cap measures against, so renewal must
    # never reset it — otherwise the cap could be slid forever.
    assert payload["ses"] == started_at
    assert payload["exp"] == original_payload["exp"] + 30


def test_renew_returns_none_for_an_invalid_token() -> None:
    svc = JWTService(secret="test")
    assert svc.renew("not-a-token") is None
    assert svc.renew("") is None


def test_renew_returns_none_once_the_absolute_cap_is_reached() -> None:
    """A hosted session must eventually go back through the Portal so the
    tier / account gate is re-evaluated, no matter how active the player is.

    The token here is still signature-valid and unexpired — the shape you get
    after the cap is tightened, or from a token minted before it existed — so
    only the cap check can stop it."""
    svc = JWTService(secret="test", ttl_seconds=3600, absolute_ttl_seconds=7200)
    over_cap = jwt.encode(
        {
            "sub": "user-1",
            "iat": _seconds_ago(60),
            "exp": _seconds_ago(-3600),
            "ses": _seconds_ago(7300),
        },
        "test",
        algorithm="HS256",
    )

    assert svc.decode(over_cap) is not None
    assert svc.renew(over_cap) is None


def test_encode_clamps_expiry_to_the_absolute_cap() -> None:
    """Otherwise the last renewal before the cap would overshoot it."""
    svc = JWTService(secret="test", ttl_seconds=3600, absolute_ttl_seconds=7200)
    started_at = _seconds_ago(7140)  # 60 seconds of session lifetime left

    payload = svc.decode(svc.encode("user-1", session_started_at=started_at))

    assert payload is not None
    assert payload["exp"] == started_at + 7200


def test_renew_anchors_legacy_tokens_without_the_session_claim_on_iat() -> None:
    """Tokens minted before sliding renewal existed must keep working."""
    svc = JWTService(secret="test", ttl_seconds=3600, absolute_ttl_seconds=7200)
    legacy = jwt.encode(
        {"sub": "user-1", "iat": _seconds_ago(60), "exp": _seconds_ago(-3600)},
        "test",
        algorithm="HS256",
    )

    renewed = svc.renew(legacy)

    assert renewed is not None
    payload = svc.decode(renewed)
    assert payload is not None
    assert payload["ses"] == pytest.approx(_seconds_ago(60), abs=2)


def test_renew_without_an_absolute_cap_never_expires_the_session() -> None:
    """Self-host default: a single-machine owner should not be logged out."""
    svc = JWTService(secret="test", ttl_seconds=3600)
    token = svc.encode("user-1", session_started_at=_seconds_ago(365 * 24 * 3600))

    assert svc.renew(token) is not None


def test_renew_rejects_a_token_without_a_subject() -> None:
    svc = JWTService(secret="test")
    subjectless = jwt.encode(
        {"iat": _seconds_ago(10), "exp": _seconds_ago(-3600)},
        "test",
        algorithm="HS256",
    )

    assert svc.renew(subjectless) is None
