"""ProactiveScheduler × BeatDueChecker integration (Phase 3 of SCENE_BEAT_PLAN).

Confirms the tick-time path:

- Tick fires → checker is asked → ``should_notify=True`` → an
  ``ARC_BEAT`` event lands on the queue → dispatcher gets called
- ``should_notify=False`` (optional beat / proactive off / nothing due)
  → no extra dispatch beyond the normal TICK sweep
- Checker crash doesn't stop the regular sweep

Tests use a tiny ``tick_seconds`` so the loop completes within
milliseconds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from kokoro_link.application.services.beat_due_checker import (
    BeatDueChecker,
    BeatScanResult,
)
from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from tests.unit._messaging_harness import build_messaging_harness, create_character


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ProactiveTrigger]] = []

    async def evaluate(
        self, *, character_id: str, trigger: ProactiveTrigger,
        now: datetime | None = None,  # noqa: ARG002
    ):
        self.calls.append((character_id, trigger))
        return None


class _StubChecker:
    """Drop-in replacement for ``BeatDueChecker`` that echoes back a
    fixed result so we don't need a full arc/event service stack."""

    def __init__(
        self,
        *,
        result: BeatScanResult = BeatScanResult.empty(),
        crash: bool = False,
    ) -> None:
        self._result = result
        self._crash = crash
        self.calls: list[str] = []

    async def scan(self, character, *, now=None) -> BeatScanResult:  # noqa: ARG002
        self.calls.append(character.id)
        if self._crash:
            raise RuntimeError("checker exploded")
        return self._result


async def _enable_proactive(harness, character_id: str) -> str:
    entity = await harness.character_repository.get(character_id)
    assert entity is not None
    updated = entity.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None, aspirations=None,
        appearance=None, proactive_enabled=True,
    )
    await harness.character_repository.save(updated)
    return updated.id


@pytest.mark.asyncio
async def test_should_notify_enqueues_arc_beat_event() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    char_id = await _enable_proactive(harness, dto.id)

    dispatcher = _RecordingDispatcher()
    checker = _StubChecker(
        result=BeatScanResult(
            attempted_beat_id="b1",
            should_notify=True,
        ),
    )
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=30.0,  # avoid second tick during the test window
        startup_grace_seconds=0.0,
        beat_due_checker=checker,  # type: ignore[arg-type]
    )
    await scheduler.start()
    try:
        # Initial tick runs immediately — give the loop a slice to
        # process the resulting ARC_BEAT enqueue.
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    assert checker.calls == [char_id]
    triggers_for_char = [
        trigger for cid, trigger in dispatcher.calls if cid == char_id
    ]
    # The initial tick fires TICK first (regular sweep), then the
    # ARC_BEAT event is consumed in a follow-up loop iteration.
    assert ProactiveTrigger.TICK in triggers_for_char
    assert ProactiveTrigger.ARC_BEAT in triggers_for_char


@pytest.mark.asyncio
async def test_should_notify_false_does_not_enqueue() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    char_id = await _enable_proactive(harness, dto.id)

    dispatcher = _RecordingDispatcher()
    checker = _StubChecker(
        result=BeatScanResult(
            attempted_beat_id="b1",
            should_notify=False,
        ),
    )
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=30.0,
        startup_grace_seconds=0.0,
        beat_due_checker=checker,  # type: ignore[arg-type]
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    triggers_for_char = [
        trigger for cid, trigger in dispatcher.calls if cid == char_id
    ]
    assert ProactiveTrigger.TICK in triggers_for_char
    assert ProactiveTrigger.ARC_BEAT not in triggers_for_char


@pytest.mark.asyncio
async def test_checker_runs_for_proactive_disabled_character() -> None:
    """World advancement is universal — the checker still runs even
    when the character won't accept proactive pings. The scheduler
    just doesn't enqueue ARC_BEAT in that case (checker handles the
    should_notify gating internally)."""
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki", proactive_enabled=False)

    dispatcher = _RecordingDispatcher()
    checker = _StubChecker(result=BeatScanResult.empty())
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=30.0,
        startup_grace_seconds=0.0,
        beat_due_checker=checker,  # type: ignore[arg-type]
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    # Checker invoked for the disabled character (world still advances).
    assert dto.id in checker.calls
    # But no proactive dispatch fired (regular TICK is also gated by
    # proactive_enabled).
    assert dto.id not in [cid for cid, _ in dispatcher.calls]


@pytest.mark.asyncio
async def test_checker_crash_does_not_break_tick_sweep() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    char_id = await _enable_proactive(harness, dto.id)

    dispatcher = _RecordingDispatcher()
    checker = _StubChecker(crash=True)
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=30.0,
        startup_grace_seconds=0.0,
        beat_due_checker=checker,  # type: ignore[arg-type]
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    # The TICK dispatch must still fire even though the checker
    # blew up — Phase 3 should never regress Phase-0 functionality.
    assert any(
        cid == char_id and trigger == ProactiveTrigger.TICK
        for cid, trigger in dispatcher.calls
    )


@pytest.mark.asyncio
async def test_scheduler_works_without_checker_wired() -> None:
    """Phase 3 is opt-in — schedulers built before the checker exists
    must keep working unchanged."""
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    char_id = await _enable_proactive(harness, dto.id)

    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=30.0,
        startup_grace_seconds=0.0,
        # No beat_due_checker.
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    assert any(
        cid == char_id and trigger == ProactiveTrigger.TICK
        for cid, trigger in dispatcher.calls
    )


@pytest.mark.asyncio
async def test_arc_beat_dropped_during_startup_grace() -> None:
    """Hot-reload defence: even when the checker says should_notify,
    the scheduler swallows the enqueue during the startup grace
    window so a crash-restart loop can't fan out duplicate pings.
    The realisation already happened — only the ping is suppressed."""
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    char_id = await _enable_proactive(harness, dto.id)

    dispatcher = _RecordingDispatcher()
    checker = _StubChecker(
        result=BeatScanResult(
            attempted_beat_id="b1",
            should_notify=True,
        ),
    )
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=30.0,
        # Long enough to cover the entire test window — every dispatch
        # should be considered "during grace".
        startup_grace_seconds=10.0,
        beat_due_checker=checker,  # type: ignore[arg-type]
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    # Checker still called (realisation must always happen).
    assert checker.calls == [char_id]
    # No dispatch fired — TICK suppressed by grace, ARC_BEAT dropped.
    assert char_id not in [cid for cid, _ in dispatcher.calls]
