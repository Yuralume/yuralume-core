"""ProactiveScheduler background schedule memorialization coverage."""

from __future__ import annotations

from datetime import datetime

import pytest

from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from tests.unit._messaging_harness import build_messaging_harness, create_character


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ProactiveTrigger]] = []

    async def evaluate(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        now: datetime | None = None,  # noqa: ARG002
    ) -> None:
        self.calls.append((character_id, trigger))


class _RecordingMemorializer:
    def __init__(self, *, crash: bool = False) -> None:
        self.calls: list[tuple[str, datetime | None]] = []
        self._crash = crash

    async def memorialize(
        self,
        *,
        character_id: str,
        now: datetime | None = None,
    ) -> int:
        self.calls.append((character_id, now))
        if self._crash:
            raise RuntimeError("memorializer exploded")
        return 0


@pytest.mark.asyncio
async def test_tick_memorializes_every_character_even_when_proactive_disabled() -> None:
    harness = build_messaging_harness()
    enabled = await create_character(harness, name="Enabled", proactive_enabled=True)
    disabled = await create_character(
        harness,
        name="Disabled",
        proactive_enabled=False,
    )
    memorializer = _RecordingMemorializer()
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        schedule_memorializer=memorializer,  # type: ignore[arg-type]
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.

    called_ids = {character_id for character_id, _ in memorializer.calls}
    assert enabled.id in called_ids
    assert disabled.id in called_ids


@pytest.mark.asyncio
async def test_memorializer_crash_does_not_break_tick_sweep() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki", proactive_enabled=True)
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        schedule_memorializer=_RecordingMemorializer(crash=True),  # type: ignore[arg-type]
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused fail-soft contract.

    assert any(
        cid == dto.id and trigger == ProactiveTrigger.TICK
        for cid, trigger in dispatcher.calls
    )


@pytest.mark.asyncio
async def test_no_memorializer_keeps_legacy_behavior() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki", proactive_enabled=True)
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused legacy contract.

    assert any(
        cid == dto.id and trigger == ProactiveTrigger.TICK
        for cid, trigger in dispatcher.calls
    )
