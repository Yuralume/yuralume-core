"""Idle-character auto-freeze sweep (CHARACTER_FREEZE_PLAN).

A cost-control reaper that freezes characters the user has stopped
interacting with. Mirrors :class:`DemoAccountReaper`'s shape: it is
driven from the proactive scheduler tick (throttled), scans the active
(non-frozen) character set once, and freezes any whose last user
interaction is older than the admin-configured idle threshold.

"Last interaction" is ``CharacterState.last_active_at`` — the single
cross-source instant the chat turn pipeline maintains, refreshed only by
real user-driven turns (never by the character's own background
activity), so a dormant character can't keep itself alive. Characters
the user has never chatted with (``last_active_at is None``) fall back to
``Character.created_at`` as the idle anchor.

Freezing is a no-op when ``auto_freeze_enabled`` is off; the admin can
still freeze individual characters immediately regardless of the flag.
Foreground chat auto-unfreezes a character, so this only ever culls
genuinely dormant ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsService,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import FREEZE_REASON_IDLE
from kokoro_link.infrastructure.app_runtime_settings.schemas import (
    CharacterFreezeRuntimeConfig,
)

_LOGGER = logging.getLogger(__name__)

_CHARACTER_FREEZE_GROUP = "character_freeze"


@dataclass(frozen=True, slots=True)
class CharacterFreezeReaperResult:
    enabled: bool = False
    scanned_characters: int = 0
    frozen_characters: int = 0
    freeze_failures: int = 0


class CharacterFreezeReaper:
    """Freeze characters idle past the configured threshold."""

    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        settings_service: AppRuntimeSettingsService,
        clock: ClockPort | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._settings_service = settings_service
        self._clock = clock

    async def run_once(
        self, *, now: datetime | None = None,
    ) -> CharacterFreezeReaperResult:
        resolved_now = self._resolve_now(now)
        config = await self._load_config()
        if not config.auto_freeze_enabled:
            return CharacterFreezeReaperResult(enabled=False)

        cutoff = resolved_now - timedelta(days=config.idle_days_threshold)
        try:
            characters = await self._character_repository.list_active()
        except Exception:
            _LOGGER.exception("character freeze reaper: list_active failed")
            return CharacterFreezeReaperResult(enabled=True)

        frozen = 0
        failures = 0
        for character in characters:
            anchor = self._idle_anchor(character)
            if anchor is None or anchor >= cutoff:
                continue
            try:
                did_freeze = await self._character_repository.set_frozen(
                    character.id,
                    frozen=True,
                    now=resolved_now,
                    reason=FREEZE_REASON_IDLE,
                )
            except Exception:
                failures += 1
                _LOGGER.exception(
                    "character freeze reaper: freeze failed character=%s",
                    character.id,
                )
                continue
            if did_freeze:
                frozen += 1

        if frozen or failures:
            _LOGGER.info(
                "character freeze reaper: scanned=%d frozen=%d failures=%d "
                "idle_days=%d",
                len(characters), frozen, failures, config.idle_days_threshold,
            )
        return CharacterFreezeReaperResult(
            enabled=True,
            scanned_characters=len(characters),
            frozen_characters=frozen,
            freeze_failures=failures,
        )

    async def _load_config(self) -> CharacterFreezeRuntimeConfig:
        try:
            config = await self._settings_service.get(
                _CHARACTER_FREEZE_GROUP,
                default=CharacterFreezeRuntimeConfig(),
            )
        except Exception:
            _LOGGER.exception(
                "character freeze reaper: config read failed; skipping sweep",
            )
            return CharacterFreezeRuntimeConfig()
        if isinstance(config, CharacterFreezeRuntimeConfig):
            return config
        # Defensive: a mis-registered group could hand back a different
        # schema. Fail closed (auto-freeze off) rather than freeze on a
        # config we can't interpret.
        return CharacterFreezeRuntimeConfig()

    @staticmethod
    def _idle_anchor(character) -> datetime | None:
        last_active = character.state.last_active_at
        anchor = last_active if last_active is not None else character.created_at
        if anchor is None:
            return None
        return ensure_utc(anchor)

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
