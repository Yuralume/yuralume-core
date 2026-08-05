"""The embedded tick drives both halves of the SC1-E idle sweep.

The per-kind due-job registry is distributed-only by construction, so a
self-host deployment sees *none* of it. Everything SC1-E promises has to
reach the embedded scheduler by another route, and these tests pin both:

* the per-character wrap-up, through the shared tick executor step;
* the once-per-tick pass over rows no character chain covers, which the
  scheduler drives itself because there is no chain to hang it on.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from tests.unit._messaging_harness import build_messaging_harness, create_character

pytestmark = pytest.mark.asyncio


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


class _RecordingCloser:
    def __init__(self, *, crash: bool = False) -> None:
        self.per_character: list[str] = []
        self.sweeps: int = 0
        self._crash = crash

    async def close_if_idle(self, character, *, now=None) -> bool:  # noqa: ANN001, ARG002
        self.per_character.append(character.id)
        return False

    async def sweep_unowned(self, *, now=None, limit=None) -> int:  # noqa: ANN001, ARG002
        self.sweeps += 1
        if self._crash:
            raise RuntimeError("sweep exploded")
        return 0


async def test_embedded_tick_wraps_up_idle_scenes_per_character() -> None:
    harness = build_messaging_harness()
    enabled = await create_character(harness, name="Enabled", proactive_enabled=True)
    # World advancement is universal: a character with proactive off can still
    # be sitting inside an abandoned scene.
    disabled = await create_character(
        harness, name="Disabled", proactive_enabled=False,
    )
    closer = _RecordingCloser()
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        story_scene_timeout_closer=closer,  # type: ignore[arg-type]
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.

    assert set(closer.per_character) >= {enabled.id, disabled.id}


async def test_embedded_tick_sweeps_rows_no_chain_covers_once() -> None:
    """Once per tick, not once per character — the query is whole-table."""
    harness = build_messaging_harness()
    await create_character(harness, name="A", proactive_enabled=True)
    await create_character(harness, name="B", proactive_enabled=True)
    closer = _RecordingCloser()
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        story_scene_timeout_closer=closer,  # type: ignore[arg-type]
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.

    assert closer.sweeps == 1


async def test_a_crashing_sweep_does_not_stop_the_tick() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="A", proactive_enabled=True)
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        story_scene_timeout_closer=_RecordingCloser(crash=True),  # type: ignore[arg-type]
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused fail-soft contract.

    assert any(
        cid == dto.id and trigger == ProactiveTrigger.TICK
        for cid, trigger in dispatcher.calls
    )


async def test_an_unwired_closer_leaves_the_tick_untouched() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="A", proactive_enabled=True)
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
