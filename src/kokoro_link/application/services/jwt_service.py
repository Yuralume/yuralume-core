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


class JWTService:
    """HS256-signed access tokens.

    The token carries:
      - ``sub`` — the user id (``operator_profiles.id``)
      - ``iat`` / ``exp`` — issued / expires (UTC seconds)

    No refresh flow — a personal-multi-user backend renewing tokens
    weekly is plenty. If a deployment needs proper rotation, we can
    layer it on top without changing the signature shape.
    """

    def __init__(
        self,
        secret: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not secret or not secret.strip():
            raise ValueError("JWT secret must be non-empty")
        self._secret = secret
        self._ttl_seconds = max(60, ttl_seconds)
        # Injected clock for tests. Default is real UTC.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def encode(self, user_id: str) -> str:
        if not user_id or not user_id.strip():
            raise ValueError("user_id must be non-empty")
        now = self._clock()
        payload = {
            "sub": user_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self._ttl_seconds)).timestamp()),
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

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
