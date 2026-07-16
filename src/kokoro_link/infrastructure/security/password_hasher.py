"""Password hashing port + bcrypt-backed adapter.

We expose a tiny port even though the implementation is a one-liner
around passlib — tests use the in-memory fake to avoid bcrypt's
intentional slowness (each hash is ~100 ms by design), and the auth
service depends on the protocol shape rather than the concrete impl
so we can swap algorithms (argon2id, etc.) without touching the
service layer.
"""

from __future__ import annotations

from typing import Protocol


class PasswordHasherPort(Protocol):
    def hash(self, plain: str) -> str:
        """Hash a plain-text password. Result is opaque + verifiable by
        :meth:`verify`. Empty / whitespace-only input raises ``ValueError``
        — callers must validate before reaching the hasher so we never
        store a credential that looks empty after trim."""

    def verify(self, plain: str, hashed: str) -> bool:
        """Constant-time compare. ``False`` on mismatch or malformed
        hash — never raise on a wrong-password attempt, only on a
        misuse (e.g. empty plain). Tolerant of a missing ``hashed``
        (returns False) because the AuthService passes the
        ``OperatorProfile.password_hash`` value directly and that can
        be NULL on the pre-setup default user."""


class BcryptPasswordHasher(PasswordHasherPort):
    """passlib bcrypt with the default 12 rounds.

    12 rounds is the passlib default (~250 ms on a 2024 laptop). High
    enough to make offline cracking expensive for the small password
    space humans pick; low enough to keep login latency under a beat.
    If a future deployment needs argon2id we can subclass-substitute
    without changing the service layer.
    """

    def __init__(self) -> None:
        # Import locally so test environments that mock the hasher
        # don't pay the bcrypt import cost. passlib's CryptContext is
        # the recommended entry-point: it handles algorithm-tag
        # round-tripping, deprecation, and pepper if we ever need it.
        from passlib.context import CryptContext

        self._ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def hash(self, plain: str) -> str:
        if not plain or not plain.strip():
            raise ValueError("password must be non-empty")
        return self._ctx.hash(plain)

    def verify(self, plain: str, hashed: str) -> bool:
        if not plain:
            return False
        if not hashed:
            return False
        try:
            return self._ctx.verify(plain, hashed)
        except Exception:
            # passlib raises on a malformed hash. AuthService treats
            # that as "credentials don't match" — surfacing the
            # exception would leak which side of the comparison was
            # broken to an attacker hitting /login with crafted inputs.
            return False


class FakePasswordHasher(PasswordHasherPort):
    """Tests-only hasher. Just prefixes the plain password with a
    marker so verify() can do a plain string compare. Avoids bcrypt's
    100ms cost on every test that touches AuthService."""

    PREFIX = "fakehash::"

    def hash(self, plain: str) -> str:
        if not plain or not plain.strip():
            raise ValueError("password must be non-empty")
        return f"{self.PREFIX}{plain}"

    def verify(self, plain: str, hashed: str) -> bool:
        if not plain or not hashed:
            return False
        return hashed == f"{self.PREFIX}{plain}"
