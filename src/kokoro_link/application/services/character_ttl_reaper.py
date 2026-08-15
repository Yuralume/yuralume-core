"""Per-tier character TTL reaper.

Deletes characters whose owning account is on a tier that sets a finite
``character_ttl_days``. That knob is pushed down per tier by the cloud
control-plane (it is one of the operator-editable Core runtime knobs), so
this reaper is **not** tied to any particular tier — an account whose
profile carries no ``character_ttl`` is simply never scanned, which is why
self-host pays nothing for it.

Historically this was the hosted demo reaper (``DemoAccountReaper``) and it
additionally released the account's Cloud demo session once its last
character was reaped. The demo site was retired on 2026-08-06 and that
release hook went with it; the TTL sweep itself is a live tier capability
and stays.
"""

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
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CharacterTtlReaperResult:
    scanned_characters: int = 0
    expired_characters: int = 0
    deleted_characters: int = 0
    delete_failures: int = 0


class CharacterTtlReaper:
    """Delete characters past their tier's ``character_ttl``."""

    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        character_service: CharacterService,
        account_runtime_profile_resolver: AccountRuntimeProfileResolverPort,
        account_runtime_usage_repository: AccountRuntimeUsageRepositoryPort,
        clock: ClockPort | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._character_service = character_service
        self._account_runtime_profile_resolver = account_runtime_profile_resolver
        self._account_runtime_usage_repository = account_runtime_usage_repository
        self._clock = clock

    async def run_once(
        self, *, now: datetime | None = None,
    ) -> CharacterTtlReaperResult:
        resolved_now = self._resolve_now(now)
        try:
            characters = await self._character_repository.list()
            create_events = await self._account_runtime_usage_repository.list_events(
                event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
                until=resolved_now,
            )
        except Exception:
            _LOGGER.exception("character ttl reaper: preflight failed")
            return CharacterTtlReaperResult()

        create_events_by_character = _index_create_events(create_events)
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
                    "character ttl reaper: runtime profile resolve failed user=%s",
                    character.user_id,
                )
                continue
            if not _is_expired(create_event, profile, resolved_now):
                continue
            expired += 1
            # H4: a tier push can land between the scan-time resolve above and
            # this delete. Re-resolve the runtime profile immediately before
            # deleting, so a just-upgraded customer's character is never reaped
            # on an in-flight sweep.
            try:
                fresh_profile = await (
                    self._account_runtime_profile_resolver.resolve_for_operator(
                        character.user_id,
                    )
                )
            except Exception:
                _LOGGER.exception(
                    "character ttl reaper: pre-delete profile re-resolve failed"
                    " user=%s",
                    character.user_id,
                )
                continue
            # Deliberately the *same* predicate as the scan above, not a weaker
            # "does the tier still set a TTL at all" check: a push that only
            # lengthens the TTL (3 days -> 30) leaves it non-``None`` while the
            # character is no longer expired, and a weaker re-check would delete
            # it anyway. Two different predicates on the two ends of the same
            # race is what makes such a bug invisible, so there is only one.
            if not _is_expired(create_event, fresh_profile, resolved_now):
                continue
            try:
                removed = await self._character_service.delete_character(
                    character.id,
                    user_id=character.user_id,
                )
            except Exception:
                delete_failures += 1
                _LOGGER.exception(
                    "character ttl reaper: character delete failed character=%s",
                    character.id,
                )
                continue
            if not removed:
                continue
            deleted += 1

        return CharacterTtlReaperResult(
            scanned_characters=len(characters),
            expired_characters=expired,
            deleted_characters=deleted,
            delete_failures=delete_failures,
        )

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


def _is_expired(
    create_event: AccountRuntimeUsageEvent,
    profile: AccountRuntimeProfile,
    now: datetime,
) -> bool:
    """Is this character past its tier's TTL as of ``now``?

    A tier with no ``character_ttl`` never expires anything, which is why
    self-host and every uncapped tier are invisible to the sweep.
    """
    if profile.character_ttl is None:
        return False
    return ensure_utc(create_event.occurred_at) + profile.character_ttl <= now


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
