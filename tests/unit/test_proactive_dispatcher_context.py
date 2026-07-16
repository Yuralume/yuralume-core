"""Verify the dispatcher threads memories + goals into the decider context.

Slice 7.2 asked for the proactive decider to see recent memories and
active goals; this test pins the wiring so a later refactor can't
silently drop it.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.application.dto.goal import CreateGoalRequest
from kokoro_link.application.services.goal_service import GoalService
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.repositories.in_memory_goals import InMemoryGoalRepository
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


class _PersonaService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_current(self, character_id: str, operator_id: str):
        self.calls.append((character_id, operator_id))
        return OperatorPersona.empty(character_id, operator_id)

    def render_for_prompt(self, persona: OperatorPersona) -> list[str]:
        return ["關於對方：目前還不熟。"]


class _CuriosityContextService:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.planned_calls: list[dict[str, object]] = []

    async def build_context(self, **kwargs):  # noqa: ANN003
        self.calls.append(SimpleNamespace(**kwargs))
        persona = kwargs["persona"]
        return SimpleNamespace(
            character_id=persona.character_id,
            operator_id=persona.operator_id,
            surface=kwargs["surface"],
        )

    async def record_planned_attempt(self, *, context, plan, now=None, **kwargs):  # noqa: ANN001, ANN003
        if not plan.should_ask:
            return None
        self.planned_calls.append({
            "context": context,
            "plan": plan,
            "now": now,
            **kwargs,
        })
        return SimpleNamespace(id="planned-attempt")


class _CuriosityPlanner:
    def __init__(self, plan: PersonaCuriosityPlan) -> None:
        self._plan = plan
        self.calls: list[object] = []

    async def plan(self, context, *, character=None):  # noqa: ANN001
        self.calls.append({"context": context, "character": character})
        return self._plan


@pytest.mark.asyncio
async def test_dispatcher_fills_memories_and_goals_into_context() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness)
    # Enable proactive and push last_active_at into the past so gate passes.
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

    # Wire a memory repo with one semantic fact, and a goal service with
    # one active goal.
    memory_repo = InMemoryMemoryRepository()
    await memory_repo.add(
        MemoryItem.create(
            character_id=character.id,
            conversation_id=None,
            kind=MemoryKind.SEMANTIC,
            content="使用者是個吉他初學者",
            salience=0.8,
            tags=[],
        ),
    )

    goal_repo = InMemoryGoalRepository()
    goal_service = GoalService(goal_repo)
    await goal_service.create_goal(
        character.id,
        CreateGoalRequest(content="陪對方練完第一首曲子", priority=4),
    )

    # Create a telegram account + proactive-accepting binding so the
    # dispatcher gets past NO_BINDING.
    account = await create_telegram_account(harness, character_id=character.id)
    await harness.binding_repository.save(
        ChannelBinding.create(
            account_id=account.id, chat_ref="c1", accepts_proactive=True,
        ),
    )

    decider = _CapturingDecider()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        memory_repository=memory_repo,
        goal_repository=goal_repo,
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert decider.last_context is not None
    assert "使用者是個吉他初學者" in decider.last_context.recent_memories_text
    assert "陪對方練完第一首曲子" in decider.last_context.active_goals_text


@pytest.mark.asyncio
async def test_dispatcher_computes_unanswered_streak_into_context() -> None:
    """Two prior pushes the user never replied to (last_active is older
    than both) must surface as unanswered_streak=2 even with a flood of
    gate-blocked rows in between — wiring guard for the anti-跳針 fact."""
    now = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    harness = build_messaging_harness()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    # User last spoke 5h ago — before both proactive pushes below.
    enabled = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=60, energy=80,
            last_active_at=now - timedelta(hours=5),
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

    attempts = InMemoryProactiveAttemptRepository()
    # Two real pushes (well outside the 30-min cooldown) the user ignored.
    await attempts.add(ProactiveAttempt.record(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT, message="第一則：今天看到一隻貓",
        now=now - timedelta(hours=3),
    ))
    await attempts.add(ProactiveAttempt.record(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT, message="第二則：在想你",
        now=now - timedelta(hours=2),
    ))
    # Flood of gate-blocked ticks since — must not bury the SENT rows.
    for i in range(30):
        await attempts.add(ProactiveAttempt.record(
            character_id=character.id, trigger=ProactiveTrigger.TICK,
            outcome=ProactiveOutcome.GATE_BLOCKED,
            now=now - timedelta(minutes=i + 1),
        ))

    decider = _CapturingDecider()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempts,
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )

    assert decider.last_context is not None
    assert decider.last_context.unanswered_streak == 2
    messages = [a.message for a in decider.last_context.recent_sent_attempts]
    assert "第一則：今天看到一隻貓" in messages
    assert "第二則：在想你" in messages


@pytest.mark.asyncio
async def test_dispatcher_adds_persona_curiosity_plan_to_proactive_context() -> None:
    now = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    harness = build_messaging_harness()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    enabled = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=60, energy=80,
            last_active_at=now - timedelta(hours=2),
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

    plan = PersonaCuriosityPlan(
        should_ask=True,
        target_layer=2,
        target_topic="companion_preference",
        tone_strategy="低壓、像朋友順口問",
        question_intent="了解對方希望角色怎麼陪伴",
        safety_reason="只碰低壓偏好",
        avoid=("不要像問卷",),
    )
    persona_service = _PersonaService()
    curiosity_context = _CuriosityContextService()
    curiosity_planner = _CuriosityPlanner(plan)
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
        operator_persona_service=persona_service,
        persona_curiosity_service=curiosity_context,
        persona_curiosity_planner=curiosity_planner,
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )

    assert decider.last_context is not None
    assert decider.last_context.persona_curiosity_plan == plan
    assert curiosity_context.calls
    assert curiosity_context.calls[0].surface == "proactive"
    assert curiosity_context.calls[0].recent_dialogue_summary == ""
    assert curiosity_planner.calls
    assert curiosity_planner.calls[0]["character"].id == character.id
    assert len(curiosity_context.planned_calls) == 1
    assert curiosity_context.planned_calls[0]["plan"] == plan
    assert curiosity_context.planned_calls[0]["now"] == now
