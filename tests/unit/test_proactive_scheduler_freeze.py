"""Proactive scheduler freeze enforcement (CHARACTER_FREEZE_PLAN).

Frozen characters must incur ZERO per-character background work, and the
idle-freeze sweep must run on its own throttle rather than every tick.
"""

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
        self.calls: list[str] = []

    async def evaluate(self, *, character_id, trigger, now=None):  # noqa: ANN001
        self.calls.append(character_id)
        return None


class _RecordingRestRecovery:
    """Rest recovery runs for every non-frozen character each tick, so it
    is a clean probe for 'did any per-character background work run'."""

    def __init__(self) -> None:
        self.refreshed: list[str] = []

    async def refresh(self, character, *, now=None, persist=True):  # noqa: ANN001
        self.refreshed.append(character.id)
        return character


class _RecordingFreezeReaper:
    def __init__(self) -> None:
        self.calls: list[datetime | None] = []

    async def run_once(self, *, now=None):  # noqa: ANN001
        self.calls.append(now)
        return None


class _DenyCharacterGuard:
    def __init__(self, denied_id: str) -> None:
        self.denied_id = denied_id

    async def is_character_allowed(self, character) -> bool:
        return character.id != self.denied_id


@pytest.mark.asyncio
async def test_frozen_character_gets_no_background_work() -> None:
    harness = build_messaging_harness()
    active = await create_character(harness, name="Active", proactive_enabled=True)
    dormant = await create_character(harness, name="Dormant", proactive_enabled=True)
    await harness.character_repository.set_frozen(
        dormant.id, frozen=True,
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    dispatcher = _RecordingDispatcher()
    rest = _RecordingRestRecovery()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        rest_recovery_refresher=rest,  # type: ignore[arg-type]
        startup_grace_seconds=0.0,
    )

    await scheduler._tick_all()

    # Frozen character: no proactive dispatch AND no rest-recovery refresh.
    assert dormant.id not in dispatcher.calls
    assert dormant.id not in rest.refreshed
    # Active character: full background work still runs.
    assert active.id in dispatcher.calls
    assert active.id in rest.refreshed


@pytest.mark.asyncio
async def test_authoritative_tenant_lock_skips_background_without_projection() -> None:
    harness = build_messaging_harness()
    active = await create_character(harness, name="Active", proactive_enabled=True)
    locked = await create_character(harness, name="Locked", proactive_enabled=True)
    dispatcher = _RecordingDispatcher()
    rest = _RecordingRestRecovery()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        rest_recovery_refresher=rest,  # type: ignore[arg-type]
        startup_grace_seconds=0.0,
        subscription_access_guard=_DenyCharacterGuard(locked.id),  # type: ignore[arg-type]
    )

    await scheduler._tick_all()

    assert locked.id not in dispatcher.calls
    assert locked.id not in rest.refreshed
    assert active.id in dispatcher.calls
    assert active.id in rest.refreshed


@pytest.mark.asyncio
async def test_freeze_sweep_runs_once_then_throttles() -> None:
    harness = build_messaging_harness()
    await create_character(harness, name="Any", proactive_enabled=True)
    clock = _FrozenClock(datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc))
    reaper = _RecordingFreezeReaper()
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        character_freeze_reaper=reaper,  # type: ignore[arg-type]
        character_freeze_sweep_interval_seconds=3600.0,
        startup_grace_seconds=0.0,
        clock=clock,  # type: ignore[arg-type]
    )

    await scheduler._tick_all()
    await scheduler._tick_all()  # same clock instant -> within throttle window

    assert len(reaper.calls) == 1


@pytest.mark.asyncio
async def test_freeze_sweep_noop_when_reaper_unwired() -> None:
    harness = build_messaging_harness()
    await create_character(harness, name="Any", proactive_enabled=True)
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
    )
    # Must not raise when no freeze reaper is wired.
    await scheduler._tick_all()
