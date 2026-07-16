"""Shared rest-recovery refresh helper.

The rest-recovery formula itself lives in
``kokoro_link.infrastructure.state.recovery`` (pure function, no I/O).
This module wraps it with the persistence + audit concerns that every
caller needs:

1. Compute the recovered state.
2. If unchanged, do nothing.
3. Otherwise persist the character with the new state and emit a
   ``SOURCE_REST_RECOVERY`` snapshot for the state-history UI.

All three call sites share this helper so the write policy stays in one
place:

* ``ChatService._load_character_with_recovery`` (chat hot path)
* ``CharacterService.list_characters`` / ``get_character`` (UI GETs)
* ``ProactiveScheduler._tick_all`` (background refresh)

Errors are swallowed with logging — recovery is background bookkeeping
and must never block the caller's primary flow.
"""

from __future__ import annotations

import logging
from datetime import datetime

from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_REST_RECOVERY,
    EmotionEvent,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.state_snapshot import SOURCE_REST_RECOVERY
from kokoro_link.infrastructure.state.recovery import apply_rest_recovery

_LOGGER = logging.getLogger(__name__)


class RestRecoveryRefresher:
    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        state_tracker=None,  # StateChangeTracker | None — imported lazily to dodge cycles
        emotion_event_repository: EmotionEventRepositoryPort | None = None,
    ) -> None:
        self._characters = character_repository
        self._state_tracker = state_tracker
        self._emotion_events = emotion_event_repository

    def set_emotion_event_repository(
        self, repository: EmotionEventRepositoryPort | None,
    ) -> None:
        """Late-bind observability storage after container repositories exist."""
        self._emotion_events = repository

    async def refresh(
        self,
        character: Character,
        *,
        now: datetime | None = None,
        persist: bool = True,
    ) -> Character:
        """Apply recovery to ``character`` and optionally persist.

        Returns the recovered character. When ``persist=False`` the DB
        is not touched — useful for read paths where we want the
        display value to be fresh but prefer the scheduler to own the
        write side.
        """
        recovered_state = apply_rest_recovery(character.state, now=now)
        if recovered_state is character.state:
            return character
        updated = character.with_state(recovered_state)
        if not persist:
            return updated
        try:
            await self._characters.save(updated)
        except Exception:
            _LOGGER.exception("rest recovery: save failed for %s", character.id)
            return updated
        if self._state_tracker is not None:
            try:
                await self._state_tracker.record(
                    character_id=character.id,
                    source=SOURCE_REST_RECOVERY,
                    before=character.state,
                    after=recovered_state,
                    now=now,
                )
            except Exception:
                _LOGGER.exception(
                    "rest recovery: snapshot failed for %s", character.id,
                )
        if self._emotion_events is not None:
            try:
                await self._emotion_events.add(EmotionEvent.new(
                    character_id=character.id,
                    operator_id=DEFAULT_OPERATOR_ID,
                    cause_ref_kind=CAUSE_REST_RECOVERY,
                    fatigue_delta=recovered_state.fatigue - character.state.fatigue,
                    energy_delta=recovered_state.energy - character.state.energy,
                    applied_to_state=True,
                    intensity=0.15,
                    emotion_label=recovered_state.emotion,
                    decay_half_life_minutes=480,
                    now=now,
                ))
            except Exception:
                _LOGGER.exception(
                    "rest recovery: emotion event failed for %s", character.id,
                )
        return updated
