"""Tick loop + event queue for the proactive scheduler.

We run the scheduler with a tiny tick so the tests don't take seconds.
The dispatcher is a fake that records every ``evaluate`` call.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from tests.unit._messaging_harness import build_messaging_harness, create_character


@dataclass(slots=True)
class _FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return ensure_utc(self.value)


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ProactiveTrigger]] = []
        self.nows: list[datetime | None] = []

    async def evaluate(
        self, *, character_id: str, trigger: ProactiveTrigger,
        now: datetime | None = None,
    ):
        self.calls.append((character_id, trigger))
        self.nows.append(now)
        return None


class _RecordingRestRecovery:
    def __init__(self) -> None:
        self.nows: list[datetime | None] = []

    async def refresh(self, character, *, now=None, persist=True):  # noqa: ANN001
        self.nows.append(now)
        return character


class _RecordingPendingFollowUps:
    def __init__(self) -> None:
        self.nows: list[datetime | None] = []

    async def tick(self, *, now=None):  # noqa: ANN001
        self.nows.append(now)
        return 0


class _RecordingPersonaDream:
    def __init__(self) -> None:
        self.should_run_nows: list[datetime | None] = []
        self.run_nows: list[datetime | None] = []

    async def should_run_now(self, character_id, operator_id, *, now=None):  # noqa: ANN001
        self.should_run_nows.append(now)
        return True

    async def run_consolidation(self, character_id, operator_id, *, now=None):  # noqa: ANN001
        self.run_nows.append(now)
        return None


class _RecordingPersonaRepository:
    async def list_characters_with_pending(self):
        return [("character-1", "operator-1")]


@pytest.mark.asyncio
async def test_notify_event_is_noop_when_scheduler_not_started() -> None:
    harness = build_messaging_harness()
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=0.05,
    )
    scheduler.notify_event(
        character_id="missing", trigger=ProactiveTrigger.POST_TURN,
    )
    # No raise, nothing stored — just returns quietly.


@pytest.mark.asyncio
async def test_tick_sweeps_proactive_enabled_characters() -> None:
    harness = build_messaging_harness()
    enabled_dto = await create_character(harness, name="Enabled")
    disabled_dto = await create_character(
        harness, name="Disabled", proactive_enabled=False,
    )
    enabled_entity = await harness.character_repository.get(enabled_dto.id)
    assert enabled_entity is not None
    enabled_pro = enabled_entity.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None, aspirations=None,
        appearance=None, proactive_enabled=True,
    )
    await harness.character_repository.save(enabled_pro)
    disabled = disabled_dto  # keep the DTO just for the id assertion below

    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=0.05,
        startup_grace_seconds=0.0,  # bypass hot-reload defence in tests
    )
    await scheduler.start()
    try:
        # Wait long enough for at least one tick.
        await asyncio.sleep(0.12)
    finally:
        await scheduler.stop()

    enabled_ids = [cid for cid, _ in dispatcher.calls]
    assert enabled_pro.id in enabled_ids
    assert disabled.id not in enabled_ids


@pytest.mark.asyncio
async def test_event_queue_fires_out_of_band() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=30.0,  # far enough we'd never tick in the test window
        startup_grace_seconds=0.0,  # bypass hot-reload defence in tests
    )
    await scheduler.start()
    try:
        scheduler.notify_event(
            character_id=character.id, trigger=ProactiveTrigger.POST_TURN,
        )
        # Give the loop a chance to pick up the event.
        await asyncio.sleep(0.1)
    finally:
        await scheduler.stop()

    assert any(
        cid == character.id and trigger == ProactiveTrigger.POST_TURN
        for cid, trigger in dispatcher.calls
    )


@pytest.mark.asyncio
async def test_tick_all_passes_clock_now_to_time_sensitive_subsystems() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness, name="Clocked")
    entity = await harness.character_repository.get(character.id)
    assert entity is not None
    await harness.character_repository.save(
        entity.update(
            name=None,
            summary=None,
            personality=None,
            interests=None,
            speaking_style=None,
            boundaries=None,
            state=None,
            aspirations=None,
            appearance=None,
            proactive_enabled=True,
        ),
    )
    frozen_now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    dispatcher = _RecordingDispatcher()
    rest = _RecordingRestRecovery()
    pending = _RecordingPendingFollowUps()
    dream = _RecordingPersonaDream()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        rest_recovery_refresher=rest,  # type: ignore[arg-type]
        pending_follow_up_dispatcher=pending,  # type: ignore[arg-type]
        persona_dream_service=dream,  # type: ignore[arg-type]
        persona_dream_repository=_RecordingPersonaRepository(),  # type: ignore[arg-type]
        startup_grace_seconds=0.0,
        clock=_FrozenClock(frozen_now),
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused clock-port contract.

    assert pending.nows == [frozen_now]
    assert rest.nows == [frozen_now]
    assert dispatcher.nows == [frozen_now]
    assert dream.should_run_nows == [frozen_now]
    assert dream.run_nows == [frozen_now]


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    harness = build_messaging_harness()
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=0.05,
    )
    await scheduler.start()
    await scheduler.stop()
    await scheduler.stop()  # no-op
