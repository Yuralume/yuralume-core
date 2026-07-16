"""Hosted demo account TTL reaper."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
    AccountRuntimeUsageEvent,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.cloud_auth import CloudDemoSessionReleasePort
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.operator_profile import OperatorProfile

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DemoAccountReaperResult:
    scanned_characters: int = 0
    expired_characters: int = 0
    deleted_characters: int = 0
    released_accounts: int = 0
    delete_failures: int = 0
    release_failures: int = 0


class DemoAccountReaper:
    """Delete expired hosted-demo characters and release their Cloud slot."""

    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        character_service: CharacterService,
        operator_profile_repository: OperatorProfileRepositoryPort,
        account_runtime_profile_resolver: AccountRuntimeProfileResolverPort,
        account_runtime_usage_repository: AccountRuntimeUsageRepositoryPort,
        release_hook: CloudDemoSessionReleasePort | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._character_service = character_service
        self._operator_profile_repository = operator_profile_repository
        self._account_runtime_profile_resolver = account_runtime_profile_resolver
        self._account_runtime_usage_repository = account_runtime_usage_repository
        self._release_hook = release_hook
        self._clock = clock

    async def run_once(self, *, now: datetime | None = None) -> DemoAccountReaperResult:
        resolved_now = self._resolve_now(now)
        try:
            characters = await self._character_repository.list()
            create_events = await self._account_runtime_usage_repository.list_events(
                event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
                until=resolved_now,
            )
        except Exception:
            _LOGGER.exception("demo account reaper: preflight failed")
            return DemoAccountReaperResult()

        create_events_by_character = _index_create_events(create_events)
        deleted_operators: set[str] = set()
        expired = 0
        deleted = 0
        delete_failures = 0

        for character in characters:
            create_event = create_events_by_character.get(character.id)
            if create_event is None:
                continue
            try:
                profile = await (
                    self._account_runtime_profile_resolver.resolve_for_operator(
                        character.user_id,
                    )
                )
            except Exception:
                _LOGGER.exception(
                    "demo account reaper: runtime profile resolve failed user=%s",
                    character.user_id,
                )
                continue
            if profile.character_ttl is None:
                continue
            if ensure_utc(create_event.occurred_at) + profile.character_ttl > resolved_now:
                continue
            expired += 1
            # H4: a paid tier push can land between the scan-time resolve above
            # and this delete. Re-resolve the runtime profile immediately
            # before deleting and skip if the operator is no longer demo (no
            # character_ttl), so a just-upgraded customer's character is never
            # reaped on an in-flight sweep.
            try:
                fresh_profile = await (
                    self._account_runtime_profile_resolver.resolve_for_operator(
                        character.user_id,
                    )
                )
            except Exception:
                _LOGGER.exception(
                    "demo account reaper: pre-delete profile re-resolve failed"
                    " user=%s",
                    character.user_id,
                )
                continue
            if fresh_profile.character_ttl is None:
                continue
            try:
                removed = await self._character_service.delete_character(
                    character.id,
                    user_id=character.user_id,
                )
            except Exception:
                delete_failures += 1
                _LOGGER.exception(
                    "demo account reaper: character delete failed character=%s",
                    character.id,
                )
                continue
            if not removed:
                continue
            deleted += 1
            deleted_operators.add(character.user_id)

        released = 0
        release_failures = 0
        for operator_id in sorted(deleted_operators):
            if await self._operator_has_remaining_characters(operator_id):
                continue
            profile = await self._get_operator_profile(operator_id)
            if profile is None:
                continue
            try:
                did_release = await self._release_demo_account(profile)
            except Exception:
                release_failures += 1
                _LOGGER.exception(
                    "demo account reaper: demo release failed operator=%s",
                    operator_id,
                )
                continue
            if did_release:
                released += 1

        return DemoAccountReaperResult(
            scanned_characters=len(characters),
            expired_characters=expired,
            deleted_characters=deleted,
            released_accounts=released,
            delete_failures=delete_failures,
            release_failures=release_failures,
        )

    async def _operator_has_remaining_characters(self, operator_id: str) -> bool:
        try:
            return bool(await self._character_repository.list_for_user(operator_id))
        except Exception:
            _LOGGER.exception(
                "demo account reaper: remaining character lookup failed user=%s",
                operator_id,
            )
            return True

    async def _get_operator_profile(
        self,
        operator_id: str,
    ) -> OperatorProfile | None:
        try:
            return await self._operator_profile_repository.get(operator_id)
        except Exception:
            _LOGGER.exception(
                "demo account reaper: operator profile lookup failed user=%s",
                operator_id,
            )
            return None

    async def _release_demo_account(self, profile: OperatorProfile) -> bool:
        if self._release_hook is None:
            return False
        if profile.auth_provider != "cloud" or profile.cloud_tenant_tier != "demo":
            return False
        if not profile.cloud_tenant_id or not profile.cloud_account_id:
            return False
        await self._release_hook.release_demo_session(
            tenant_id=profile.cloud_tenant_id,
            account_id=profile.cloud_account_id,
        )
        return True

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


def _index_create_events(
    events: list[AccountRuntimeUsageEvent],
) -> dict[str, AccountRuntimeUsageEvent]:
    indexed: dict[str, AccountRuntimeUsageEvent] = {}
    for event in events:
        if not event.resource_id:
            continue
        current = indexed.get(event.resource_id)
        if current is None or event.occurred_at < current.occurred_at:
            indexed[event.resource_id] = event
    return indexed
