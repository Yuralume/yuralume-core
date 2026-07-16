"""BDD: proactive dispatcher must apply lazy rest recovery.

The chat path applies ``apply_rest_recovery`` at the top of
``ChatService._load_character_with_recovery``, so energy replenishes
whenever the user actually talks. But the proactive scheduler runs
independently — if a character went to bed exhausted and the user
didn't message them, the DB state stays at energy=0 forever, and the
heuristic gate (``energy <= 15`` → block) refuses to let a proactive
message through the next morning.

Fix: ``ProactiveDispatcher.evaluate`` applies the same recovery
immediately after loading the character, persists the updated state,
and records a ``SOURCE_REST_RECOVERY`` snapshot so the state-history
UI shows the crept-up numbers. With recovery applied, a fresh morning
tick lets the gate through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.application.services.rest_recovery_refresher import (
    RestRecoveryRefresher,
)
from kokoro_link.application.services.state_tracker import StateChangeTracker
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.state_snapshot import SOURCE_REST_RECOVERY
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_state_history import (
    InMemoryStateHistoryRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_telegram_account,
)


class _YesDecider:
    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        return ProactiveDecision(
            should_send=True,
            reason="ok",
            message="早安",
        )


class _NullDecider:
    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        return ProactiveDecision(should_send=False, reason="null")


@dataclass
class _Fixture:
    harness: Any
    dispatcher: ProactiveDispatcher
    attempts: InMemoryProactiveAttemptRepository
    history: InMemoryStateHistoryRepository


async def _build_fixture(
    *,
    last_active: datetime,
    fatigue: int,
    energy: int,
    decider: Any,
) -> _Fixture:
    harness = build_messaging_harness()
    character = await harness.character_repository.get(
        (await harness.character_service.create_character(
            _req("Nia"),
        )).id,
    )
    assert character is not None

    exhausted_state = CharacterState(
        emotion="tired",
        affection=50,
        fatigue=fatigue,
        trust=50,
        energy=energy,
        last_active_at=last_active,
    )
    await harness.character_repository.save(
        character.update_state(exhausted_state)
        if hasattr(character, "update_state")
        else _with_state(character, exhausted_state),
    )

    # Opt-in: proactive + binding that accepts proactive.
    updated = await harness.character_repository.get(character.id)
    assert updated is not None
    updated = _with_proactive(updated, enabled=True)
    await harness.character_repository.save(updated)

    account = await create_telegram_account(
        harness, character_id=character.id,
    )
    binding = await harness.binding_service.create(
        account_id=account.id, chat_ref="chat-1",
    )
    # Flip accepts_proactive to True — the service default is False.
    flipped = binding.with_accepts_proactive(True) if hasattr(
        binding, "with_accepts_proactive",
    ) else _with_accepts_proactive(binding, True)
    await harness.binding_repository.save(flipped)

    attempts = InMemoryProactiveAttemptRepository()
    history = InMemoryStateHistoryRepository()
    tracker = StateChangeTracker(history)
    refresher = RestRecoveryRefresher(
        character_repository=harness.character_repository,
        state_tracker=tracker,
    )
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempts,
        gate=HeuristicProactiveGate(local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        state_tracker=tracker,
        rest_recovery_refresher=refresher,
    )
    return _Fixture(
        harness=harness, dispatcher=dispatcher,
        attempts=attempts, history=history,
    )


def _req(name: str):
    from kokoro_link.application.dto.character import CreateCharacterRequest
    return CreateCharacterRequest(name=name)


def _with_state(character: Character, state: CharacterState) -> Character:
    return character.with_state(state)


def _with_proactive(character: Character, *, enabled: bool) -> Character:
    return character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        proactive_enabled=enabled,
    )


def _with_accepts_proactive(binding, value: bool):
    from dataclasses import replace
    return replace(binding, accepts_proactive=value)


@pytest.mark.asyncio
async def test_overnight_idle_recovers_energy_before_gate_check() -> None:
    """前一晚精力歸零、12 小時沒互動 → evaluate 應該先做 recovery，
    gate 應該通過，DB 的 energy 也會被寫上新值。"""
    last_night = datetime(2026, 4, 18, 23, 0, tzinfo=timezone.utc)
    morning = datetime(2026, 4, 19, 11, 0, tzinfo=timezone.utc)

    f = await _build_fixture(
        last_active=last_night,
        fatigue=95,
        energy=0,
        decider=_YesDecider(),
    )
    character_id = (await f.harness.character_repository.list())[0].id

    attempt = await f.dispatcher.evaluate(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        now=morning,
    )

    # Recovery applied → gate no longer sees energy=0, decider said yes.
    assert attempt.outcome == ProactiveOutcome.SENT, attempt.reason

    # DB state must reflect the new energy (half-life 4h → 12h = 3 halves →
    # fatigue ≈ 95 * (1/8) ≈ 12; energy_deficit = 100 * (1/8) ≈ 12 → energy ≈ 88).
    persisted = await f.harness.character_repository.get(character_id)
    assert persisted is not None
    assert persisted.state.energy >= 80
    assert persisted.state.fatigue <= 20

    # History records a REST_RECOVERY snapshot.
    snapshots = await f.history.query(character_id, limit=10)
    assert any(s.source == SOURCE_REST_RECOVERY for s in snapshots)


@pytest.mark.asyncio
async def test_recent_activity_keeps_exhausted_state() -> None:
    """剛剛才互動過 → recovery 不會跑，energy 仍 = 0，gate 會擋下。"""
    when = datetime(2026, 4, 19, 11, 0, tzinfo=timezone.utc)
    recent = when - timedelta(minutes=2)

    f = await _build_fixture(
        last_active=recent,
        fatigue=95,
        energy=0,
        decider=_YesDecider(),
    )
    character_id = (await f.harness.character_repository.list())[0].id

    attempt = await f.dispatcher.evaluate(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        now=when,
    )

    # Gate blocks — idle threshold (user is active) kicks in first, so
    # the proactive send is refused regardless of the (tiny) recovery.
    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED

    # Recovery is continuous, so 2 minutes of decay nudges energy by ~1.
    # The important invariant: the character is still in exhausted
    # territory, not magically back to full energy.
    persisted = await f.harness.character_repository.get(character_id)
    assert persisted is not None
    assert persisted.state.energy <= 15
    assert persisted.state.fatigue >= 80


@pytest.mark.asyncio
async def test_no_op_recovery_skips_save_and_snapshot() -> None:
    """fatigue=0 且 energy=100 時，recovery 不會產生變化，不應寫 snapshot。"""
    long_ago = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 19, 11, 0, tzinfo=timezone.utc)

    f = await _build_fixture(
        last_active=long_ago,
        fatigue=0,
        energy=100,
        decider=_NullDecider(),
    )
    character_id = (await f.harness.character_repository.list())[0].id

    await f.dispatcher.evaluate(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        now=now,
    )

    snapshots = await f.history.query(character_id, limit=10)
    assert all(s.source != SOURCE_REST_RECOVERY for s in snapshots)
