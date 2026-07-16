from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.quiet_hours_service import QuietHoursService
from kokoro_link.application.services.emotion_aggregator import (
    ExponentialDecayEmotionAggregator,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.time import SystemClock


@dataclass(slots=True)
class _FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return ensure_utc(self.value)

    def advance(self, delta: timedelta) -> None:
        self.value = ensure_utc(self.value + delta)


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=["溫柔"],
        interests=[],
        speaking_style="自然",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _prompt_kwargs() -> dict:
    character = _character()
    return {
        "character": character,
        "conversation": Conversation.start(character_id=character.id),
        "recent_messages": [],
        "memories": [],
        "pending_state": character.state,
        "latest_user_message": "嗨",
    }


def test_system_clock_returns_aware_utc_datetime() -> None:
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_prompt_builder_uses_clock_when_now_is_omitted() -> None:
    clock = _FrozenClock(datetime(2026, 4, 18, 23, 30, tzinfo=timezone.utc))
    builder = DefaultPromptContextBuilder(
        local_tz=ZoneInfo("Asia/Taipei"),
        clock=clock,
    )

    prompt = builder.build(**_prompt_kwargs())

    assert "現在時間" in prompt
    assert "2026-04-19 07:30" in prompt


@pytest.mark.asyncio
async def test_quiet_hours_uses_clock_when_now_is_omitted() -> None:
    clock = _FrozenClock(datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc))
    service = QuietHoursService(
        preferences=InMemoryPreferencesRepository(),
        env_start=14,
        env_end=17,
        clock=clock,
    )

    assert await service.in_quiet_hours()

    clock.advance(timedelta(hours=4))
    assert not await service.in_quiet_hours()


@pytest.mark.asyncio
async def test_three_day_fast_forward_drives_subjective_time_cooldown_and_emotion_decay() -> None:
    base = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    clock = _FrozenClock(base + timedelta(days=3))
    prompt = DefaultPromptContextBuilder(clock=clock).build(
        **_prompt_kwargs(),
        idle_minutes=3 * 24 * 60,
    )
    assert "天前" in prompt

    character = _character().update(
        name=None,
        summary=None,
        personality=None,
        interests=None,
        speaking_style=None,
        boundaries=None,
        state=None,
        aspirations=None,
        appearance=None,
        proactive_cooldown_minutes=60,
    )
    gate = HeuristicProactiveGate()
    blocked = await gate.check(
        character=character,
        trigger=ProactiveTrigger.TICK,
        now=base + timedelta(minutes=30),
        sent_today=0,
        last_attempt_at=base,
        idle_minutes=120,
        current_activity=None,
    )
    passed = await gate.check(
        character=character,
        trigger=ProactiveTrigger.TICK,
        now=clock.now(),
        sent_today=0,
        last_attempt_at=base,
        idle_minutes=3 * 24 * 60,
        current_activity=None,
    )
    assert not blocked.passed
    assert passed.passed

    event = EmotionEvent(
        id="e1",
        character_id=character.id,
        operator_id="default",
        cause_ref_kind="turn",
        affection_delta=20,
        intensity=1.0,
        decay_half_life_minutes=60,
        created_at=base,
    )
    snapshot = ExponentialDecayEmotionAggregator().derive(
        events=[event],
        baseline_affection=50,
        baseline_fatigue=0,
        baseline_trust=50,
        baseline_energy=100,
        baseline_emotion="neutral",
        now=clock.now(),
    )
    assert snapshot.affection == 50
