"""Auth orchestration — setup, login, user CRUD.

Sits between the auth router and the operator-profile repository.
Pure application service: no HTTP, no DB, just orchestrate the port
calls and enforce business rules.

The ``KOKORO_AUTH_ENABLED=false`` mode does NOT skip this service —
the route layer's dependency simply hands back the default operator
without ever calling :meth:`verify_token`. AuthService stays usable
even in disabled mode for the rare CLI / admin path that wants to
provision users ahead of flipping the switch.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from kokoro_link.application.exceptions import (
    InvalidCredentials,
    PermissionDenied,
    SetupAlreadyComplete,
    SetupNotAllowed,
    UserAlreadyExists,
    UserNotFound,
)
from kokoro_link.application.services.jwt_service import JWTService
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    DEFAULT_PRIMARY_LANGUAGE,
    OperatorProfile,
    normalise_language_tag,
)
from kokoro_link.domain.value_objects.timezone import (
    DEFAULT_TIMEZONE_ID,
    normalise_timezone_id,
)
from kokoro_link.infrastructure.security.password_hasher import PasswordHasherPort


_LOGGER = logging.getLogger(__name__)


class AuthService:
    def __init__(
        self,
        repository: OperatorProfileRepositoryPort,
        hasher: PasswordHasherPort,
        jwt_service: JWTService,
        default_timezone_id: str = DEFAULT_TIMEZONE_ID,
    ) -> None:
        self._repo = repository
        self._hasher = hasher
        self._jwt = jwt_service
        self._default_timezone_id = normalise_timezone_id(default_timezone_id)

    # ------------------------------------------------------------------
    # config / status helpers
    # ------------------------------------------------------------------

    async def needs_setup(self) -> bool:
        """``True`` when the default user has no password yet — front-
        end uses this to route to /setup on first run.

        Returning ``True`` for "default user row missing" too: that
        means migration ct5y7z00070 didn't run / failed, and the only
        sensible UI affordance is "do setup", which will also create
        the row."""
        default = await self._repo.get(DEFAULT_OPERATOR_ID)
        if default is None:
            return True
        return not default.has_password()

    # ------------------------------------------------------------------
    # setup — bootstrap the first admin
    # ------------------------------------------------------------------

    async def setup_initial_admin(
        self,
        *,
        email: str,
        password: str,
        primary_language: str = DEFAULT_PRIMARY_LANGUAGE,
        timezone_id: str | None = None,
        country_code: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        location_label: str | None = None,
    ) -> tuple[OperatorProfile, str]:
        """Attach email/password to the default user, promote to admin.

        Refuses if the default user already has a password — that's
        what :class:`SetupAlreadyComplete` is for. Race-safety lives at
        the repository: ``set_default_password_if_unset`` runs a
        conditional update with ``WHERE password_hash IS NULL`` so two
        concurrent setup requests can't both win.

        ``primary_language`` is the BCP 47 tag chosen at setup time
        (see FRONTEND_I18N_PLAN §使用者主要語言). It rides on the same
        atomic update as the credentials so the operator's content
        language is pinned at the same moment the account becomes real.
        Once written it's immutable; downstream LLM prompts read it
        from the operator entity.
        """
        normalised_email = _normalise_email(email)
        if not normalised_email:
            raise InvalidCredentials()
        if not password or not password.strip():
            raise InvalidCredentials()
        # ``normalise_language_tag`` raises ValueError on structurally
        # broken tags. For HTTP callers Pydantic validators on
        # ``SetupRequest`` short-circuit this with 422 before reaching
        # the service; this guard catches direct-call paths (CLI, tests)
        # so the entity-layer invariant is enforced at the service
        # boundary too.
        normalised_language = normalise_language_tag(primary_language)
        normalised_timezone = normalise_timezone_id(
            timezone_id or self._default_timezone_id,
        )

        default = await self._repo.get(DEFAULT_OPERATOR_ID)
        if default is None:
            raise SetupNotAllowed(
                "default user row missing — run alembic upgrade head",
            )
        if default.has_password():
            raise SetupAlreadyComplete()

        password_hash = self._hasher.hash(password)
        winner = await self._repo.set_default_password_if_unset(
            email=normalised_email,
            password_hash=password_hash,
            is_admin=True,
            primary_language=normalised_language,
            timezone_id=normalised_timezone,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            location_label=location_label,
        )
        if winner is None:
            # Another concurrent setup request beat us to it — the row
            # already has credentials. Surface as "already done" so the
            # client can switch to the login flow.
            raise SetupAlreadyComplete()
        token = self._jwt.encode(winner.id)
        return winner, token

    # ------------------------------------------------------------------
    # login
    # ------------------------------------------------------------------

    async def login(
        self, *, email: str, password: str,
    ) -> tuple[OperatorProfile, str]:
        normalised = _normalise_email(email)
        if not normalised or not password:
            raise InvalidCredentials()
        user = await self._repo.get_by_email(normalised)
        if user is None or not user.has_password():
            raise InvalidCredentials()
        if not self._hasher.verify(password, user.password_hash or ""):
            raise InvalidCredentials()
        token = self._jwt.encode(user.id)
        return user, token

    # ------------------------------------------------------------------
    # token verify (used by dependency)
    # ------------------------------------------------------------------

    async def verify_token(self, token: str) -> OperatorProfile | None:
        """Return the user identified by ``token``, or ``None``.

        ``None`` covers all of: malformed token, expired token, valid
        signature but referenced user no longer exists. The dependency
        layer collapses every None into the same 401 — distinguishing
        them at this layer would leak.
        """
        user_id = self._jwt.user_id_from(token)
        if not user_id:
            return None
        return await self._repo.get(user_id)

    # ------------------------------------------------------------------
    # admin: list / create / delete
    # ------------------------------------------------------------------

    async def list_users(self, *, actor: OperatorProfile) -> list[OperatorProfile]:
        _require_admin(actor)
        return await self._repo.list_all()

    async def create_user(
        self,
        *,
        actor: OperatorProfile,
        email: str,
        password: str,
        display_name: str,
        is_admin: bool = False,
        primary_language: str = DEFAULT_PRIMARY_LANGUAGE,
        timezone_id: str | None = None,
        country_code: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        location_label: str | None = None,
    ) -> OperatorProfile:
        _require_admin(actor)
        normalised_email = _normalise_email(email)
        if not normalised_email:
            raise InvalidCredentials()
        if not password or not password.strip():
            raise InvalidCredentials()
        if not display_name or not display_name.strip():
            raise InvalidCredentials()
        # See ``setup_initial_admin`` — Pydantic catches bad tags for
        # HTTP callers; this re-runs the invariant for direct callers.
        normalised_language = normalise_language_tag(primary_language)
        normalised_timezone = normalise_timezone_id(
            timezone_id or self._default_timezone_id,
        )

        existing = await self._repo.get_by_email(normalised_email)
        if existing is not None:
            raise UserAlreadyExists(normalised_email)

        new_user = OperatorProfile(
            id=str(uuid.uuid4()),
            display_name=display_name.strip(),
            email=normalised_email,
            password_hash=self._hasher.hash(password),
            is_admin=is_admin,
            primary_language=normalised_language,
            timezone_id=normalised_timezone,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            location_label=location_label,
        )
        await self._repo.save(new_user)
        return new_user

    async def delete_user(
        self, *, actor: OperatorProfile, user_id: str,
    ) -> bool:
        _require_admin(actor)
        if actor.id == user_id:
            raise PermissionDenied("cannot delete yourself")
        target = await self._repo.get(user_id)
        if target is None:
            raise UserNotFound(user_id)
        if target.is_admin:
            # Last-admin guard: count remaining admins after this
            # delete; refuse if it would leave the system locked out.
            all_users = await self._repo.list_all()
            remaining_admins = sum(
                1 for u in all_users
                if u.is_admin and u.id != user_id
            )
            if remaining_admins == 0:
                raise PermissionDenied(
                    "cannot delete the last admin",
                )
        return await self._repo.delete(user_id)

    async def set_user_admin(
        self, *, actor: OperatorProfile, user_id: str, is_admin: bool,
    ) -> OperatorProfile:
        """Promote / demote an existing user's admin flag.

        Recovery path for the common "created a second account but forgot to
        tick Grant admin" case: admin surfaces (BYOK / provider keys, models,
        site settings) gate on ``is_admin`` alone, so a missed checkbox
        otherwise forces deleting and recreating the user. Mirrors
        ``delete_user``'s last-admin guard so the install can never demote
        itself into a lockout.
        """
        _require_admin(actor)
        target = await self._repo.get(user_id)
        if target is None:
            raise UserNotFound(user_id)
        if target.is_admin and not is_admin:
            all_users = await self._repo.list_all()
            remaining_admins = sum(
                1 for u in all_users
                if u.is_admin and u.id != user_id
            )
            if remaining_admins == 0:
                raise PermissionDenied("cannot demote the last admin")
        updated = target.update(is_admin=is_admin)
        await self._repo.save(updated)
        return updated

    async def change_password(
        self,
        *,
        actor: OperatorProfile,
        user_id: str,
        new_password: str,
    ) -> OperatorProfile:
        """Admin updates a user's password, or user updates their own.

        Non-admin caller may only change their own; admin may change
        anyone's. Empty / whitespace password rejected up front."""
        if not new_password or not new_password.strip():
            raise InvalidCredentials()
        if actor.id != user_id and not actor.is_admin:
            raise PermissionDenied("cannot change another user's password")
        target = await self._repo.get(user_id)
        if target is None:
            raise UserNotFound(user_id)
        updated = target.update(
            password_hash=self._hasher.hash(new_password),
        )
        await self._repo.save(updated)
        return updated

    async def change_own_password(
        self,
        *,
        actor: OperatorProfile,
        current_password: str,
        new_password: str,
    ) -> OperatorProfile:
        """Update the current user's own password after re-checking
        their current password.

        This is the player-facing "change password" path. Admin reset
        intentionally stays separate in :meth:`change_password` so an
        admin can recover accounts without knowing the old secret.
        """
        if not current_password or not current_password.strip():
            raise InvalidCredentials()
        if not new_password or not new_password.strip():
            raise InvalidCredentials()
        target = await self._repo.get(actor.id)
        if target is None:
            raise UserNotFound(actor.id)
        if not target.has_password():
            raise InvalidCredentials()
        if not self._hasher.verify(
            current_password, target.password_hash or "",
        ):
            raise InvalidCredentials()
        updated = target.update(
            password_hash=self._hasher.hash(new_password),
        )
        await self._repo.save(updated)
        return updated


# ----------------------------------------------------------------------
# module-private helpers
# ----------------------------------------------------------------------


def _normalise_email(raw: str) -> str:
    return (raw or "").strip().lower()


def _require_admin(actor: OperatorProfile) -> None:
    if not actor.is_admin:
        raise PermissionDenied("admin required")
