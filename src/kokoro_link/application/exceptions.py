"""Application-layer exception types.

Kept in one module so multiple services can raise the same identity
class (lets the API layer translate once into HTTP status codes
without each route handler enumerating service-specific exceptions).
"""

from __future__ import annotations


class CharacterNotOwned(Exception):
    """Raised when a request touches a ``character_id`` that doesn't
    belong to the current user (MULTI_USER_AUTH_PLAN Batch 2).

    The API layer translates this to **404**, not 403 — leaking
    existence ("yes the character exists but it's not yours") would
    let a multi-tenant deployment enumerate ids. Same surface as
    "character not found" from the caller's perspective.

    Owner guard ``ownership.ensure_character_owned`` is the single
    raise site so the message wording stays consistent."""

    def __init__(self, character_id: str) -> None:
        super().__init__(f"character {character_id} not accessible")
        self.character_id = character_id


class AuthError(Exception):
    """Base for auth-related rejections that the API translates to 401
    or 4xx. Specific subclasses carry the HTTP intent."""


class InvalidCredentials(AuthError):
    """Wrong email or password at login. Translated to 401. The
    message is intentionally generic so attackers can't tell email
    from password failures apart."""

    def __init__(self) -> None:
        super().__init__("invalid email or password")


class SetupAlreadyComplete(AuthError):
    """``/auth/setup`` called after the default user already has a
    password. Translated to 409 so the front-end can route to /login
    instead of /setup."""

    def __init__(self) -> None:
        super().__init__("initial setup already complete")


class SetupNotAllowed(AuthError):
    """``/auth/setup`` cannot proceed — typically because the default
    user row is missing (migration not run). Translated to 503."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class DemoSessionUnavailable(AuthError):
    """Public demo session could not be created for a known demo reason."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


class PermissionDenied(AuthError):
    """Non-admin caller attempted an admin-only operation, or attempted
    to delete themselves / the last admin. Translated to 403."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class UserAlreadyExists(AuthError):
    """``create_user`` called with an email already in use. Translated
    to 409."""

    def __init__(self, email: str) -> None:
        super().__init__(f"user with email {email} already exists")
        self.email = email


class UserNotFound(AuthError):
    """Lookup by id that should have succeeded came up empty (admin
    operations on a deleted user, etc.). Translated to 404."""

    def __init__(self, user_id: str) -> None:
        super().__init__(f"user {user_id} not found")
        self.user_id = user_id
