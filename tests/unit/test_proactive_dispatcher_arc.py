"""ProactiveDispatcher ensures the active arc before building context.

Mirrors ChatService: newly-evaluated characters that have no arc yet
should get one lazily created so the decider sees the same narrative
anchor as user-initiated chat. Without this the decider only gets
gacha events + dialogue summary and will generate openers ignoring
the arc the user is progressing through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.story_arc import (
    ARC_ACTIVE,
    TENSION_SETUP,
    StoryArc,
    StoryArcBeat,
)
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


@dataclass
class _FakeArcService:
    arc: StoryArc | None
    calls: int = 0
    last_today: date | None = None

    async def ensure_active_arc(self, character, *, today=None, auto_start=True):
        self.calls += 1
        self.last_today = today
        return self.arc


class _OperatorProfileService:
    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name=user_id,
            timezone_id="Asia/Taipei",
        )


def _build_arc(character_id: str, today: date) -> StoryArc:
    beat_today = StoryArcBeat.create(
        arc_id="arc-1",
        sequence=0,
        scheduled_date=today,
        title="起點",
        summary="角色決定踏出第一步",
        tension=TENSION_SETUP,
    )
    beat_tomorrow = StoryArcBeat.create(
        arc_id="arc-1",
        sequence=1,
        scheduled_date=today + timedelta(days=1),
        title="小插曲",
        summary="遇到意料外的轉折",
        tension=TENSION_SETUP,
    )
    return StoryArc.create(
        id="arc-1",
        character_id=character_id,
        title="新的開始",
        premise="角色正在學著信任對方，慢慢放下過去的陰影。",
        theme="friendship",
        start_date=today,
        end_date=today + timedelta(days=7),
        beats=(beat_today, beat_tomorrow),
        status=ARC_ACTIVE,
    )


async def _prepare_harness():
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
    return harness, character


def _dispatcher(
    harness, *, decider, story_arc_service, operator_profile_service=None,
):
    return ProactiveDispatcher(
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
        story_arc_service=story_arc_service,
        operator_profile_service=operator_profile_service,
    )


@pytest.mark.asyncio
async def test_dispatcher_threads_active_arc_into_context() -> None:
    harness, character = await _prepare_harness()
    today = datetime.now(timezone.utc).date()
    arc = _build_arc(character.id, today)
    arc_service = _FakeArcService(arc=arc)
    decider = _CapturingDecider()

    dispatcher = _dispatcher(
        harness, decider=decider, story_arc_service=arc_service,
    )
    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert arc_service.calls == 1
    ctx = decider.last_context
    assert ctx is not None
    assert ctx.active_arc is arc
    # Two beats exist (today + tomorrow); forward_beats(limit=2, include_today=True) returns both.
    assert len(ctx.upcoming_beats) == 2
    assert ctx.upcoming_beats[0].title == "起點"


@pytest.mark.asyncio
async def test_dispatcher_without_arc_service_leaves_arc_empty() -> None:
    harness, character = await _prepare_harness()
    decider = _CapturingDecider()
    dispatcher = _dispatcher(harness, decider=decider, story_arc_service=None)

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    ctx = decider.last_context
    assert ctx is not None
    assert ctx.active_arc is None
    assert ctx.upcoming_beats == ()


@pytest.mark.asyncio
async def test_dispatcher_arc_service_returning_none_is_tolerated() -> None:
    harness, character = await _prepare_harness()
    decider = _CapturingDecider()
    arc_service = _FakeArcService(arc=None)

    dispatcher = _dispatcher(
        harness, decider=decider, story_arc_service=arc_service,
    )
    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert arc_service.calls == 1
    ctx = decider.last_context
    assert ctx is not None
    assert ctx.active_arc is None
    assert ctx.upcoming_beats == ()


@pytest.mark.asyncio
async def test_dispatcher_threads_owner_local_day_to_arc_lookup() -> None:
    harness, character = await _prepare_harness()
    from dataclasses import replace
    character = await harness.character_repository.get(character.id)
    assert character is not None
    # This test pins ``now`` to a fixed past instant so the Asia/Taipei
    # civil date equals ``owner_today``. ``_prepare_harness`` anchors
    # ``last_active_at`` on the real wall clock, which is *after* that
    # fixed ``now`` — so idle would clamp to 0 and the gate would block
    # before the arc lookup is ever reached. Re-anchor the last-active
    # instant a couple of hours before the fixed ``now`` to keep the
    # user "idle enough" for the gate to pass, matching what the harness
    # does relative to real-now.
    fixed_now = datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc)
    character = replace(
        character,
        user_id="owner-tw",
        state=replace(
            character.state, last_active_at=fixed_now - timedelta(hours=2),
        ),
    )
    await harness.character_repository.save(character)

    owner_today = date(2026, 6, 15)
    arc = _build_arc(character.id, owner_today)
    arc_service = _FakeArcService(arc=arc)
    decider = _CapturingDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        story_arc_service=arc_service,
        operator_profile_service=_OperatorProfileService(),
    )

    await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=fixed_now,
    )

    assert fixed_now.astimezone(
        ZoneInfo("Asia/Taipei"),
    ).date() == owner_today
    assert arc_service.last_today == owner_today
    ctx = decider.last_context
    assert ctx is not None
    assert ctx.upcoming_beats[0].scheduled_date == owner_today
