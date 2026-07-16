"""In-memory ``OperatorProfileRepositoryPort`` for tests / fake provider."""

from __future__ import annotations

import asyncio

from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
    normalise_language_tag,
)
from kokoro_link.domain.value_objects.timezone import normalise_timezone_id


class InMemoryOperatorProfileRepository(OperatorProfileRepositoryPort):
    def __init__(self) -> None:
        self._profiles: dict[str, OperatorProfile] = {}
        # ``set_default_password_if_unset`` mirrors the SQL conditional
        # update under a lock so concurrent setup requests in tests see
        # the same single-winner behaviour as a real DB.
        self._setup_lock = asyncio.Lock()

    async def get(self, operator_id: str) -> OperatorProfile | None:
        return self._profiles.get(operator_id)

    async def get_default(self) -> OperatorProfile | None:
        return self._profiles.get(DEFAULT_OPERATOR_ID)

    async def get_by_email(self, email: str) -> OperatorProfile | None:
        normalised = email.strip().lower()
        if not normalised:
            return None
        for profile in self._profiles.values():
            if profile.email == normalised:
                return profile
        return None

    async def get_by_cloud_account_id(
        self, cloud_account_id: str,
    ) -> OperatorProfile | None:
        normalised = cloud_account_id.strip()
        if not normalised:
            return None
        for profile in self._profiles.values():
            if profile.cloud_account_id == normalised:
                return profile
        return None

    async def list_by_cloud_tenant_id(
        self, cloud_tenant_id: str,
    ) -> list[OperatorProfile]:
        normalised = cloud_tenant_id.strip()
        if not normalised:
            return []
        return [
            profile
            for profile in self._profiles.values()
            if profile.cloud_tenant_id == normalised
        ]

    async def set_cloud_tenant_tier_for_cloud_tenant(
        self, cloud_tenant_id: str, tier: str,
    ) -> int:
        normalised_tenant = (cloud_tenant_id or "").strip()
        if not normalised_tenant:
            return 0
        normalised_tier = (tier or "").strip().lower()
        if not normalised_tier:
            return 0
        # ``replace`` re-runs ``__post_init__`` which normalises the tier, so
        # stored values stay consistent with the SA repo / entity load path.
        from dataclasses import replace
        updated = 0
        for operator_id, profile in list(self._profiles.items()):
            if (
                profile.cloud_tenant_id == normalised_tenant
                and profile.auth_provider == "cloud"
            ):
                self._profiles[operator_id] = replace(
                    profile, cloud_tenant_tier=normalised_tier,
                )
                updated += 1
        return updated

    async def list_all(self) -> list[OperatorProfile]:
        return list(self._profiles.values())

    async def save(self, profile: OperatorProfile) -> None:
        self._profiles[profile.id] = profile

    async def delete(self, operator_id: str) -> bool:
        if operator_id in self._profiles:
            del self._profiles[operator_id]
            return True
        return False

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
        async with self._setup_lock:
            existing = self._profiles.get(DEFAULT_OPERATOR_ID)
            if existing is None:
                return None
            if existing.has_password():
                return None
            # ``OperatorProfile.update`` deliberately doesn't expose
            # ``primary_language`` (immutable after registration). For
            # the one-time setup transition we use ``dataclasses.replace``
            # directly — this is the only path allowed to set the value.
            from dataclasses import replace
            updated = replace(
                existing,
                email=email.strip().lower(),
                password_hash=password_hash,
                is_admin=is_admin,
                primary_language=primary_language,
                timezone_id=timezone_id,
                country_code=country_code,
                latitude=latitude,
                longitude=longitude,
                location_label=location_label,
            )
            self._profiles[DEFAULT_OPERATOR_ID] = updated
            return updated

    async def set_default_locale_if_unconfigured(
        self,
        *,
        primary_language: str | None = None,
        timezone_id: str | None = None,
    ) -> OperatorProfile | None:
        async with self._setup_lock:
            existing = self._profiles.get(DEFAULT_OPERATOR_ID)
            if existing is None or existing.has_password():
                return None
            changes: dict[str, str] = {}
            if primary_language is not None:
                lang = normalise_language_tag(primary_language)
                if lang != existing.primary_language:
                    changes["primary_language"] = lang
            if timezone_id is not None:
                tz = normalise_timezone_id(timezone_id)
                if tz != existing.timezone_id:
                    changes["timezone_id"] = tz
            if not changes:
                return None
            # ``OperatorProfile.update`` deliberately hides
            # ``primary_language`` / ``timezone_id`` (immutable post-setup).
            # The boot-time seed is the other allowed writer for the still-
            # unconfigured row, mirroring ``set_default_password_if_unset``.
            from dataclasses import replace
            updated = replace(existing, **changes)
            self._profiles[DEFAULT_OPERATOR_ID] = updated
            return updated
