"""SQLAlchemy-backed ``OperatorProfileRepositoryPort``.

Tiny table, tiny repo. Nothing fancy: one row per operator, ``save``
upserts, no batch APIs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
    normalise_language_tag,
)
from kokoro_link.domain.value_objects.timezone import normalise_timezone_id
from kokoro_link.infrastructure.persistence.models import OperatorProfileRow


class SAOperatorProfileRepository(OperatorProfileRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, operator_id: str) -> OperatorProfile | None:
        async with self._session_factory() as session:
            row = await session.get(OperatorProfileRow, operator_id)
            if row is None:
                return None
            return _row_to_entity(row)

    async def get_default(self) -> OperatorProfile | None:
        return await self.get(DEFAULT_OPERATOR_ID)

    async def save(self, profile: OperatorProfile) -> None:
        now = datetime.now(timezone.utc)
        aliases_payload = json.dumps(list(profile.aliases), ensure_ascii=False)
        async with self._session_factory() as session:
            existing = await session.get(OperatorProfileRow, profile.id)
            if existing is None:
                session.add(OperatorProfileRow(
                    id=profile.id,
                    display_name=profile.display_name,
                    display_name_locked=profile.display_name_locked,
                    aliases_json=aliases_payload,
                    pronouns=profile.pronouns,
                    email=profile.email,
                    password_hash=profile.password_hash,
                    is_admin=profile.is_admin,
                    primary_language=profile.primary_language,
                    timezone_id=profile.timezone_id,
                    current_status=profile.current_status,
                    current_status_set_at=profile.current_status_set_at,
                    country_code=profile.country_code,
                    latitude=profile.latitude,
                    longitude=profile.longitude,
                    location_label=profile.location_label,
                    cloud_account_id=profile.cloud_account_id,
                    cloud_tenant_id=profile.cloud_tenant_id,
                    cloud_tenant_tier=profile.cloud_tenant_tier,
                    auth_provider=profile.auth_provider,
                    created_at=now,
                    updated_at=now,
                ))
            else:
                existing.display_name = profile.display_name
                existing.display_name_locked = profile.display_name_locked
                existing.aliases_json = aliases_payload
                existing.pronouns = profile.pronouns
                existing.email = profile.email
                existing.password_hash = profile.password_hash
                existing.is_admin = profile.is_admin
                # primary_language is intentionally NOT updated on
                # subsequent saves — see OperatorProfile.update for
                # why. Insert path above pins the initial value.
                # timezone_id is the same pinned identity setting:
                # normal profile saves must not reinterpret historical
                # civil dates/times by overwriting an existing row.
                existing.current_status = profile.current_status
                existing.current_status_set_at = profile.current_status_set_at
                existing.country_code = profile.country_code
                existing.latitude = profile.latitude
                existing.longitude = profile.longitude
                existing.location_label = profile.location_label
                existing.cloud_account_id = profile.cloud_account_id
                existing.cloud_tenant_id = profile.cloud_tenant_id
                # cloud_tenant_tier is intentionally NOT updated on ordinary
                # saves — same pinning as primary_language/timezone_id above.
                # Tier is authoritative ONLY via the dedicated push path
                # (set_cloud_tenant_tier_for_cloud_tenant) and the INSERT
                # branch. Overwriting it here would let a login re-projection
                # carrying a stale tier revert a committed paid push (H3
                # lost-update).
                existing.auth_provider = profile.auth_provider
                existing.updated_at = now
            await session.commit()

    async def get_by_email(self, email: str) -> OperatorProfile | None:
        """Find a profile by normalised email. Used by AuthService at
        login time. Comparison is case-insensitive — the entity already
        lower-cases on construction, so we lower-case the lookup too to
        stay consistent."""
        from sqlalchemy import select
        normalised = email.strip().lower()
        if not normalised:
            return None
        async with self._session_factory() as session:
            result = await session.execute(
                select(OperatorProfileRow).where(
                    OperatorProfileRow.email == normalised
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_entity(row)

    async def get_by_cloud_account_id(
        self, cloud_account_id: str,
    ) -> OperatorProfile | None:
        from sqlalchemy import select
        normalised = cloud_account_id.strip()
        if not normalised:
            return None
        async with self._session_factory() as session:
            result = await session.execute(
                select(OperatorProfileRow).where(
                    OperatorProfileRow.cloud_account_id == normalised
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_entity(row)

    async def list_by_cloud_tenant_id(
        self, cloud_tenant_id: str,
    ) -> list[OperatorProfile]:
        """Every operator projected under one Cloud tenant. Empty for a
        blank / unknown tenant (the Cloud→Core freeze batch treats that
        as "no operators to touch")."""
        from sqlalchemy import select
        normalised = cloud_tenant_id.strip()
        if not normalised:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(OperatorProfileRow).where(
                    OperatorProfileRow.cloud_tenant_id == normalised
                )
            )
            return [_row_to_entity(r) for r in result.scalars().all()]

    async def set_cloud_tenant_tier_for_cloud_tenant(
        self, cloud_tenant_id: str, tier: str,
    ) -> int:
        """Bulk-project a new tier onto every cloud operator of a tenant.

        One authoritative UPDATE guarded by ``auth_provider == 'cloud'`` so a
        local operator that happens to share the tenant key is never touched.
        Returns the number of rows written (0 for a blank tenant / tier).
        Tier is normalised the same way the entity does on load so the
        resolver's ``== 'demo'`` / control-plane lookups keep matching."""
        from sqlalchemy import update

        normalised_tenant = (cloud_tenant_id or "").strip()
        if not normalised_tenant:
            return 0
        normalised_tier = (tier or "").strip().lower()
        if not normalised_tier:
            return 0
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            stmt = (
                update(OperatorProfileRow)
                .where(OperatorProfileRow.cloud_tenant_id == normalised_tenant)
                .where(OperatorProfileRow.auth_provider == "cloud")
                .values(cloud_tenant_tier=normalised_tier, updated_at=now)
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    async def list_all(self) -> list[OperatorProfile]:
        """List every operator profile — used by admin user CRUD."""
        from sqlalchemy import select
        async with self._session_factory() as session:
            result = await session.execute(select(OperatorProfileRow))
            return [_row_to_entity(r) for r in result.scalars().all()]

    async def delete(self, operator_id: str) -> bool:
        """Hard-delete an operator. ON DELETE CASCADE on characters.user_id
        wipes their characters and (transitively) all character-scoped
        rows. AuthService is expected to enforce business rules first
        (no self-delete, no last-admin delete)."""
        async with self._session_factory() as session:
            row = await session.get(OperatorProfileRow, operator_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

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

        Drives the WHERE clause to ``password_hash IS NULL`` so two
        concurrent setup requests can't both stamp credentials. Returns
        the post-update row when this caller won; ``None`` otherwise.

        ``primary_language`` and ``timezone_id`` are set on this same
        atomic update so the winner's registration choices stick; later
        loads of the default row see the post-setup values. Once set,
        AuthService never rewrites them (immutable after registration).
        """
        from sqlalchemy import update

        now = datetime.now(timezone.utc)
        normalised = email.strip().lower()
        async with self._session_factory() as session:
            stmt = (
                update(OperatorProfileRow)
                .where(OperatorProfileRow.id == DEFAULT_OPERATOR_ID)
                .where(OperatorProfileRow.password_hash.is_(None))
                .values(
                    email=normalised,
                    password_hash=password_hash,
                    is_admin=is_admin,
                    primary_language=primary_language,
                    timezone_id=timezone_id,
                    country_code=country_code,
                    latitude=latitude,
                    longitude=longitude,
                    location_label=location_label,
                    updated_at=now,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            if result.rowcount == 0:
                return None
            row = await session.get(OperatorProfileRow, DEFAULT_OPERATOR_ID)
            if row is None:  # pragma: no cover — defensive
                return None
            return _row_to_entity(row)

    async def set_default_locale_if_unconfigured(
        self,
        *,
        primary_language: str | None = None,
        timezone_id: str | None = None,
    ) -> OperatorProfile | None:
        """Set ``primary_language`` / ``timezone_id`` on the default row
        while it is still unconfigured. Reads first so only fields that
        actually differ are written, then guards the UPDATE with
        ``password_hash IS NULL`` so a registration that landed between the
        read and write is never overwritten. Returns ``None`` when the row
        is missing, configured, or already on the requested values."""
        from sqlalchemy import update

        async with self._session_factory() as session:
            row = await session.get(OperatorProfileRow, DEFAULT_OPERATOR_ID)
            if row is None or row.password_hash is not None:
                return None
            values: dict[str, object] = {}
            if primary_language is not None:
                lang = normalise_language_tag(primary_language)
                if lang != row.primary_language:
                    values["primary_language"] = lang
            if timezone_id is not None:
                tz = normalise_timezone_id(timezone_id)
                if tz != row.timezone_id:
                    values["timezone_id"] = tz
            if not values:
                return None
            values["updated_at"] = datetime.now(timezone.utc)
            stmt = (
                update(OperatorProfileRow)
                .where(OperatorProfileRow.id == DEFAULT_OPERATOR_ID)
                .where(OperatorProfileRow.password_hash.is_(None))
                .values(**values)
            )
            result = await session.execute(stmt)
            await session.commit()
            if result.rowcount == 0:  # raced with a setup that set a password
                return None
            refreshed = await session.get(OperatorProfileRow, DEFAULT_OPERATOR_ID)
            if refreshed is None:  # pragma: no cover — defensive
                return None
            return _row_to_entity(refreshed)


def _row_to_entity(row: OperatorProfileRow) -> OperatorProfile:
    try:
        aliases_raw = json.loads(row.aliases_json) if row.aliases_json else []
    except json.JSONDecodeError:
        # Corrupt row — surface as "no aliases" rather than crash the
        # read path. Next save will rewrite it cleanly.
        aliases_raw = []
    aliases = tuple(
        str(alias) for alias in aliases_raw
        if isinstance(alias, (str, int, float)) and str(alias).strip()
    )
    return OperatorProfile(
        id=row.id,
        display_name=row.display_name,
        display_name_locked=bool(getattr(row, "display_name_locked", False)),
        aliases=aliases,
        pronouns=row.pronouns,
        email=row.email,
        password_hash=row.password_hash,
        is_admin=bool(row.is_admin),
        primary_language=row.primary_language or "zh-TW",
        timezone_id=getattr(row, "timezone_id", None) or "UTC",
        current_status=getattr(row, "current_status", None),
        current_status_set_at=getattr(row, "current_status_set_at", None),
        country_code=getattr(row, "country_code", None),
        latitude=getattr(row, "latitude", None),
        longitude=getattr(row, "longitude", None),
        location_label=getattr(row, "location_label", None),
        cloud_account_id=getattr(row, "cloud_account_id", None),
        cloud_tenant_id=getattr(row, "cloud_tenant_id", None),
        cloud_tenant_tier=getattr(row, "cloud_tenant_tier", None) or "standard",
        auth_provider=getattr(row, "auth_provider", None) or "local",
    )
