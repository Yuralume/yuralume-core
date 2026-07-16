"""ProactiveDispatcher surfaces just_finished_activity in the context.

When a proactive tick lands in a schedule gap, the decider needs to
know what wrapped up just before, not only what comes next — otherwise
the generated message ignores the character's morning and sounds
like it woke up at that exact moment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
)


class _CapturingDecider(ProactiveDeciderPort):
    def __init__(self) -> None:
        self.last_context: ProactiveContext | None = None

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.last_context = context
        return ProactiveDecision(False, "inspection only", None)


@pytest.mark.asyncio
async def test_dispatcher_threads_just_finished_from_resolver() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    enabled = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=60, energy=80,
            last_active_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ),
        proactive_enabled=True,
    )
    await harness.character_repository.save(enabled)
    account = await create_telegram_account(harness, character_id=character.id)
    await harness.binding_repository.save(
        ChannelBinding.create(
            account_id=account.id, chat_ref="c1", accepts_proactive=True,
        ),
    )

    now = datetime.now(timezone.utc)
    just_finished = ScheduleActivity.create(
        start_at=now - timedelta(hours=1, minutes=30),
        end_at=now - timedelta(minutes=30),
        description="午餐會面",
        category="social",
        location="公司附近",
        busy_score=0.4,
    )

    async def resolver(_character, _when):
        return None, [], None, just_finished

    decider = _CapturingDecider()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        schedule_resolver=resolver,
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    ctx = decider.last_context
    assert ctx is not None
    assert ctx.current_activity is None
    assert ctx.just_finished_activity is just_finished
