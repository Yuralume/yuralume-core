"""Synchronise an authoritative Cloud tenant subscription lock into Core.

The tenant-level state is committed first, so access guards fail closed even
when no operator has logged in yet, a character is created concurrently, or a
projection write fails. The following fan-out only maintains each
character's retryable ``subscription_locked`` projection, which lets
background scans skip locked tenants cheaply. Idle/manual character-freeze
provenance remains an independent concern and is never overwritten here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.cloud_subscription import (
    CloudSubscriptionRepositoryPort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubscriptionFreezeResult:
    """Outcome of one tenant-wide freeze / unfreeze batch.

    ``operators`` is how many operators the tenant fanned out to;
    ``frozen`` / ``unfrozen`` count characters actually flipped this run;
    ``failures`` counts per-character repository errors that were skipped
    (the batch does not abort on them)."""

    operators: int = 0
    frozen: int = 0
    unfrozen: int = 0
    failures: int = 0


class SubscriptionFreezeService:
    """Persist tenant access state, then converge character projections."""

    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        operator_profile_repository: OperatorProfileRepositoryPort,
        subscription_repository: CloudSubscriptionRepositoryPort,
        clock: ClockPort,
    ) -> None:
        self._character_repository = character_repository
        self._operator_profile_repository = operator_profile_repository
        self._subscription_repository = subscription_repository
        self._clock = clock

    async def freeze_all_for_cloud_tenant(
        self, tenant_id: str,
    ) -> SubscriptionFreezeResult:
        """Lock ``tenant_id`` and converge existing character projections.

        Projection failures are counted for retry/alerting, but do not undo
        the authoritative tenant lock. Re-running is idempotent."""
        now = self._resolve_now()
        await self._subscription_repository.set_locked(
            tenant_id, locked=True, updated_at=now,
        )
        operators = await self._operators_for_tenant(tenant_id)
        frozen = 0
        failures = 0
        for operator in operators:
            for character in await self._characters_for(operator.id):
                if character.subscription_locked:
                    continue
                try:
                    did_freeze = (
                        await self._character_repository.set_subscription_locked(
                            character.id, locked=True,
                        )
                    )
                except Exception:
                    failures += 1
                    _LOGGER.exception(
                        "subscription freeze: freeze failed character=%s "
                        "tenant=%s",
                        character.id, tenant_id,
                    )
                    continue
                if did_freeze:
                    frozen += 1
        # Always emit the outcome (even operators=0 / frozen=0) so an
        # unexpectedly-empty batch — e.g. a tenant whose operators were never
        # projected with cloud_tenant_id — is visible rather than silent.
        _LOGGER.info(
            "subscription freeze: tenant=%s operators=%d frozen=%d "
            "failures=%d",
            tenant_id, len(operators), frozen, failures,
        )
        return SubscriptionFreezeResult(
            operators=len(operators), frozen=frozen, failures=failures,
        )

    async def unfreeze_subscription_lapse_for_cloud_tenant(
        self, tenant_id: str,
    ) -> SubscriptionFreezeResult:
        """Unlock ``tenant_id`` and clear its character projections.

        Idle-sweep and manual admin freezes remain untouched. Projection
        failures are reported so the caller can retry convergence."""
        now = self._resolve_now()
        await self._subscription_repository.set_locked(
            tenant_id, locked=False, updated_at=now,
        )
        operators = await self._operators_for_tenant(tenant_id)
        unfrozen = 0
        failures = 0
        for operator in operators:
            for character in await self._characters_for(operator.id):
                if not character.subscription_locked:
                    continue
                try:
                    did_unfreeze = (
                        await self._character_repository.set_subscription_locked(
                            character.id, locked=False,
                        )
                    )
                except Exception:
                    failures += 1
                    _LOGGER.exception(
                        "subscription freeze: unfreeze failed character=%s "
                        "tenant=%s",
                        character.id, tenant_id,
                    )
                    continue
                if did_unfreeze:
                    unfrozen += 1
        _LOGGER.info(
            "subscription freeze: tenant=%s operators=%d unfrozen=%d "
            "failures=%d",
            tenant_id, len(operators), unfrozen, failures,
        )
        return SubscriptionFreezeResult(
            operators=len(operators), unfrozen=unfrozen, failures=failures,
        )

    async def _operators_for_tenant(self, tenant_id: str):
        return await self._operator_profile_repository.list_by_cloud_tenant_id(
            tenant_id,
        )

    async def _characters_for(self, operator_id: str):
        return await self._character_repository.list_for_user(operator_id)

    def _resolve_now(self) -> datetime:
        return ensure_utc(self._clock.now())
