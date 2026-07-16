"""Startup grace window defends against rapid-restart double-fires."""

import asyncio
from datetime import datetime

import pytest

from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from tests.unit._messaging_harness import build_messaging_harness, create_character


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ProactiveTrigger]] = []

    async def evaluate(
        self, *, character_id: str, trigger: ProactiveTrigger,
        now: datetime | None = None,
    ):
        self.calls.append((character_id, trigger))
        return None


@pytest.mark.asyncio
async def test_tick_during_grace_window_is_skipped() -> None:
    """The 00:53 bug — restart ticks immediately and fires. Grace stops it."""
    harness = build_messaging_harness()
    char = await create_character(harness, name="GraceGuarded")
    entity = await harness.character_repository.get(char.id)
    assert entity is not None
    proactive = entity.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None, aspirations=None,
        appearance=None, proactive_enabled=True,
    )
    await harness.character_repository.save(proactive)

    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=0.05,
        startup_grace_seconds=60.0,  # realistic default
    )
    await scheduler.start()
    try:
        # Let a few ticks happen — all within grace window.
        await asyncio.sleep(0.2)
    finally:
        await scheduler.stop()

    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_event_during_grace_window_is_dropped() -> None:
    """POST_TURN events racing a fresh start also get grace-dropped."""
    harness = build_messaging_harness()
    character = await create_character(harness)
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=30.0,
        startup_grace_seconds=60.0,
    )
    await scheduler.start()
    try:
        scheduler.notify_event(
            character_id=character.id, trigger=ProactiveTrigger.POST_TURN,
        )
        await asyncio.sleep(0.1)
    finally:
        await scheduler.stop()

    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_grace_zero_disables_the_window() -> None:
    harness = build_messaging_harness()
    char = await create_character(harness)
    entity = await harness.character_repository.get(char.id)
    assert entity is not None
    proactive = entity.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None, aspirations=None,
        appearance=None, proactive_enabled=True,
    )
    await harness.character_repository.save(proactive)

    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=0.05,
        startup_grace_seconds=0.0,
    )
    await scheduler.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    assert len(dispatcher.calls) >= 1
