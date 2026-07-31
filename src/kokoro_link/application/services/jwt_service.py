"""JWT encode/decode service.

Symmetric HS256 — the secret stays on the backend, the front-end only
sees the opaque token. We could split into a port + adapter like the
password hasher, but JWT is a stable spec with no I/O surface, so the
extra layer would be ceremony. Tests construct with a fixed secret +
a frozen clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import jwt


_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Seconds since epoch at which the *session* (not this particular token)
# began. Renewal carries it forward untouched so the absolute cap measures
# real session age rather than the age of the newest token.
_SESSION_ANCHOR_CLAIM = "ses"


class JWTService:
    """HS256-signed access tokens with sliding renewal.

    The token carries:
      - ``sub`` — the user id (``operator_profiles.id``)
      - ``iat`` / ``exp`` — issued / expires (UTC seconds)
      - ``ses`` — when the session began (survives renewal)

    :meth:`renew` slides a still-valid token forward so a player who is
    actively using the app is never evicted mid-session. ``absolute_ttl_seconds``
    bounds how far that can go: hosted sessions must eventually return through
    the Portal so the account/tier gate is re-evaluated. Zero (the self-host
    default) means unbounded renewal — a single-machine owner should not be
    logged out on a timer.
    """

    def __init__(
        self,
        secret: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        absolute_ttl_seconds: int = 0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not secret or not secret.strip():
            raise ValueError("JWT secret must be non-empty")
        self._secret = secret
        self._ttl_seconds = max(60, ttl_seconds)
        self._absolute_ttl_seconds = max(0, absolute_ttl_seconds)
        # Injected clock for tests. Default is real UTC.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def encode(self, user_id: str, *, session_started_at: int | None = None) -> str:
        if not user_id or not user_id.strip():
            raise ValueError("user_id must be non-empty")
        now = self._clock()
        issued_at = int(now.timestamp())
        anchor = issued_at if session_started_at is None else session_started_at
        payload = {
            "sub": user_id,
            "iat": issued_at,
            "exp": self._expiry_for(now, anchor),
            _SESSION_ANCHOR_CLAIM: anchor,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def renew(self, token: str) -> str | None:
        """Re-issue a still-valid token, or ``None`` if it may not be renewed.

        ``None`` means "send the user back through sign-in": the token is
        invalid/expired, carries no subject, or its session has outlived
        :attr:`absolute_ttl_seconds`. Callers map all three to the same
        401 — the distinction is not useful to a client and leaking it
        would tell an attacker which tokens were once real.
        """
        payload = self.decode(token)
        if not payload:
            return None
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            return None
        anchor = self._session_anchor(payload)
        if self._absolute_ttl_seconds:
            age = int(self._clock().timestamp()) - anchor
            if age >= self._absolute_ttl_seconds:
                return None
        return self.encode(subject, session_started_at=anchor)

    def _expiry_for(self, now: datetime, session_started_at: int) -> int:
        """Sliding expiry, clamped so the last renewal cannot overshoot the cap."""
        expiry = int((now + timedelta(seconds=self._ttl_seconds)).timestamp())
        if not self._absolute_ttl_seconds:
            return expiry
        return min(expiry, session_started_at + self._absolute_ttl_seconds)

    @staticmethod
    def _session_anchor(payload: dict) -> int:
        """Session start, falling back to ``iat`` for tokens minted before the
        claim existed — an upgrade must not invalidate in-flight sessions.

        A payload carrying neither is anchored at the epoch, which fails the
        cap check closed rather than granting an unbounded session."""
        for claim in (_SESSION_ANCHOR_CLAIM, "iat"):
            value = payload.get(claim)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return 0

    def decode(self, token: str) -> dict | None:
        """Return the decoded payload, or ``None`` on any failure.

        We swallow every PyJWT exception and turn them into ``None``
        because the only thing the caller (the FastAPI dependency)
        cares about is "valid or not". Surfacing the exception would
        mean each call site has to handle ``ExpiredSignature`` /
        ``InvalidToken`` separately, and the answer is always the
        same — 401.
        """
        if not token:
            return None
        try:
            return jwt.decode(token, self._secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None

    def user_id_from(self, token: str) -> str | None:
        """Short-cut for the common dependency path: token → user_id."""
        payload = self.decode(token)
        if not payload:
            return None
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            return None
        return sub
