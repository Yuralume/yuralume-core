"""ProactiveScheduler × ScheduleService eager pre-generation.

Verifies that each tick calls ``ensure_schedule`` for every character,
including characters whose proactive messages are disabled, so a user who
hasn't opened the app today still gets today's schedule materialised in
the background.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

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


class _RecordingScheduleService:
    """Stub that captures rolling-window calls without touching the
    real planner / repository."""

    def __init__(self, *, crash: bool = False) -> None:
        self.calls: list[tuple[str, date | None]] = []
        self._crash = crash

    async def ensure_schedule(
        self, character, *, date_: date | None = None,
    ):
        self.calls.append((character.id, date_))
        if self._crash:
            raise RuntimeError("planner exploded")
        return None

    async def ensure_window(
        self, character, *, start: date | None = None, days: int = 3,
    ):
        # Record one call per (character, day-in-window). The
        # production scheduler delegates per-day to ensure_schedule;
        # this mirrors that without coupling the test to internal
        # iteration order.
        self.calls.append((character.id, start))
        if self._crash:
            raise RuntimeError("planner exploded")
        return []


@pytest.mark.asyncio
async def test_tick_pre_generates_schedule_for_every_character() -> None:
    harness = build_messaging_harness()
    proactive_dto = await create_character(
        harness, name="Proactive", proactive_enabled=True,
    )
    silent_dto = await create_character(
        harness, name="Silent", proactive_enabled=False,
    )

    # Keep one enabled and one disabled to confirm both kinds still receive
    # ensure_schedule (schedule is "world advances", not gated on
    # proactive_enabled).

    schedule_stub = _RecordingScheduleService()
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=0.05,
        startup_grace_seconds=0.0,
        schedule_service=schedule_stub,  # type: ignore[arg-type]
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.12)
    finally:
        await scheduler.stop()

    called_ids = {cid for cid, _ in schedule_stub.calls}
    assert proactive_dto.id in called_ids
    assert silent_dto.id in called_ids


@pytest.mark.asyncio
async def test_tick_continues_when_schedule_service_crashes() -> None:
    """A planner crash for one character must not abort the rest of the
    tick — other characters' downstream steps (proactive dispatch) still
    need to run."""
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    entity = await harness.character_repository.get(dto.id)
    assert entity is not None
    enabled = entity.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None, aspirations=None,
        appearance=None, proactive_enabled=True,
    )
    await harness.character_repository.save(enabled)

    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=0.05,
        startup_grace_seconds=0.0,
        schedule_service=_RecordingScheduleService(crash=True),  # type: ignore[arg-type]
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.12)
    finally:
        await scheduler.stop()

    assert any(cid == enabled.id for cid, _ in dispatcher.calls)


@pytest.mark.asyncio
async def test_no_schedule_service_keeps_legacy_behavior() -> None:
    """``schedule_service=None`` is the pre-eager default — the
    scheduler must still tick and dispatch."""
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    entity = await harness.character_repository.get(dto.id)
    assert entity is not None
    enabled = entity.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None, aspirations=None,
        appearance=None, proactive_enabled=True,
    )
    await harness.character_repository.save(enabled)

    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=0.05,
        startup_grace_seconds=0.0,
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.12)
    finally:
        await scheduler.stop()

    assert any(cid == enabled.id for cid, _ in dispatcher.calls)
