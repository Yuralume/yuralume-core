"""``OperatorProfileRepositoryPort`` — persistence boundary for the
human operator's profile.

Phase 1 design notes:

- ``get_default()`` is the common path; almost every caller wants
  "the singleton operator". Exposed as its own method so callers
  don't have to know ``DEFAULT_OPERATOR_ID`` exists.
- ``get(id)`` is the seam for the eventual multi-operator world. The
  implementation today is "look up by primary key" but the call site
  reads naturally for that future.
- ``save`` is upsert: callers don't have to distinguish create from
  update. The entity is small and self-contained, so we don't expose
  partial-update semantics at the port level.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.operator_profile import OperatorProfile


class OperatorProfileRepositoryPort(Protocol):
    async def get(self, operator_id: str) -> OperatorProfile | None:
        """Return the operator with this id, or ``None`` if not stored."""

    async def get_default(self) -> OperatorProfile | None:
        """Return the default singleton operator, or ``None`` if the
        operator has never saved a profile.

        Service layer treats ``None`` as "use ``OperatorProfile.default()``"
        — this method does NOT auto-create the row, so callers can tell
        "operator hasn't set a name yet" apart from "operator picked
        the literal placeholder name as their actual name"."""

    async def get_by_email(self, email: str) -> OperatorProfile | None:
        """Find an operator by login email (MULTI_USER_AUTH_PLAN Batch 2).

        Email comparison is case-insensitive; implementations normalise
        both sides. Returns ``None`` for unknown email so the login flow
        can answer with a generic 401 without revealing which side
        (email vs password) was wrong."""

    async def get_by_cloud_account_id(
        self, cloud_account_id: str,
    ) -> OperatorProfile | None:
        """Find the local cloud projection for a Yuralume account."""

    async def list_by_cloud_tenant_id(
        self, cloud_tenant_id: str,
    ) -> list[OperatorProfile]:
        """List every operator projected under one Yuralume Cloud tenant.

        Drives the Cloud→Core subscription-freeze batch: a tenant's tier
        downgrade must freeze / thaw the characters of *all* operators
        belonging to that tenant. ``cloud_tenant_id`` is the stable key
        stamped at login projection (``CloudFederatedAuthStrategy``);
        returns an empty list for an unknown / blank tenant."""

    async def set_cloud_tenant_tier_for_cloud_tenant(
        self, cloud_tenant_id: str, tier: str,
    ) -> int:
        """Push a new subscription tier onto every cloud operator of a tenant.

        Drives the Cloud→Core tier-sync bridge so a tier change takes effect
        without waiting for the operator to re-login (the projection is
        otherwise only refreshed at login by ``CloudFederatedAuthStrategy``).
        Updates only rows with this ``cloud_tenant_id`` **and**
        ``auth_provider == 'cloud'`` — local operators are never touched.
        Returns the number of operator rows written. Idempotent (a blank /
        unknown tenant updates nothing and returns 0)."""

    async def list_all(self) -> list[OperatorProfile]:
        """Enumerate every operator profile — used by admin user CRUD."""

    async def save(self, profile: OperatorProfile) -> None:
        """Upsert the operator profile. Idempotent."""

    async def set_default_password_if_unset(
        self,
        *,
        email: str,
        password_hash: str,
        is_admin: bool = True,
        primary_language: str = "zh-TW",
        timezone_id: str = "UTC",
        country_code: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        location_label: str | None = None,
    ) -> OperatorProfile | None:
        """Atomic conditional update for first-time admin setup.

        Bind ``email`` / ``password_hash`` / ``is_admin`` /
        ``primary_language`` / ``timezone_id`` to the default operator
        row **only if** the row currently has no ``password_hash``.
        Returns the post-update entity when this caller won the race,
        ``None`` when another setup request beat us to it.

        Implementations must drive the WHERE clause to ``password_hash
        IS NULL`` so two concurrent setup requests can't both succeed
        with different secrets (review §P1-4). ``primary_language`` is
        stamped on the same atomic update so the winner's choices stick;
        both values are then immutable for the row's lifetime.
        """

    async def set_default_locale_if_unconfigured(
        self,
        *,
        primary_language: str | None = None,
        timezone_id: str | None = None,
    ) -> OperatorProfile | None:
        """Stamp the deploy-time default language and/or timezone on the
        default operator row **only while it is still unconfigured** (no
        ``password_hash``).

        This is the boot-time seed path for single-user self-host: it lets
        ``USER_PRIMARY_LANGUAGE`` / ``USER_TIMEZONE`` drive the default
        operator's content + UI language and civil-time interpretation
        without going through ``/auth/setup``. Implementations must drive
        the WHERE clause to ``password_hash IS NULL`` so a real
        registration (which atomically pins both via
        ``set_default_password_if_unset``) is never overwritten — both stay
        immutable for the row's lifetime once setup runs.

        Only the provided fields are touched, and only when they differ
        from the stored value. Returns the post-update entity when at least
        one field changed this call, or ``None`` when there is nothing to
        do: the row is missing, already configured (has a password), or
        already on the requested values. Idempotent — a second boot on the
        same values writes nothing."""

    async def delete(self, operator_id: str) -> bool:
        """Hard-delete the operator. ``True`` if a row was removed.

        Implementations rely on ON DELETE CASCADE on
        ``characters.user_id`` (and transitively on the character-
        scoped tables) so a single call wipes every owned row.
        Business rules — "you can't delete yourself", "you can't
        delete the last admin" — are enforced by AuthService, not by
        the repository."""
