"""CF1 / P1a — cross-day commitments land already-confirmed.

Real defect (COMMITMENT_LIFECYCLE_AND_FRESHNESS_PLAN §0 loop A, §2 P1a):
on 2026-07-26 the user explicitly agreed in chat to "明天早上一起去刨冰
店", yet the activity that landed on the 7/27 schedule still carried
``operator_invite_pending`` — so downstream every surface kept treating a
settled plan as an unanswered invitation.

Root cause is a reachability gap, not a parsing bug: the prompt's upgrade
rule routes confirmation through ``modify``, and ``modify`` is only
honoured for activity ids present in *today's* schedule
(``known_activity_ids``). A future-day seed has no id the model can see,
so the upgrade could never reach it. ``test_modify_cannot_reach_a_future
_day_activity`` pins that gap; the fix is a template rule that makes the
``add`` itself carry ``operator_confirmed_shared``.

These tests run the post-turn parser and the schedule service back to
back, because the deliverable is about what ends up *persisted* on the
future day — parsing the field alone would not prove the seed carries it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.post_turn.llm_processor import LLMPostTurnProcessor
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc

# The reported case, kept verbatim so the scenario stays recognisable.
_TODAY = date(2026, 7, 26)
_TOMORROW = date(2026, 7, 27)


class _ScriptedModel:
    provider_id = "scripted"

    def __init__(self, response: str) -> None:
        self._response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response

    async def generate_stream(self, prompt: str):  # pragma: no cover
        if False:
            yield ""


class _UnusedPlanner:
    """apply_adjustments never plans — fail loudly if that changes."""

    async def plan_day(self, **_: object) -> DailySchedule:  # pragma: no cover
        raise AssertionError("apply_adjustments must not invoke the planner")


class _FrozenScheduleService(ScheduleService):
    """ScheduleService with today() pinned so "future" stays future."""

    def today(self, now: datetime | None = None) -> date:
        return _TODAY


def _character() -> Character:
    return Character.create(
        name="芊璃",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _operator_ref(activity: ScheduleActivity) -> ParticipantRef | None:
    for ref in activity.participant_refs:
        if ref.actor_kind == "operator":
            return ref
    return None


def _today_schedule(
    character_id: str,
    activities: list[ScheduleActivity] | None = None,
) -> DailySchedule:
    return DailySchedule.create(
        character_id=character_id,
        date_=_TODAY,
        activities=activities or [],
        is_planned=True,
    )


def _shared_invite_activity() -> ScheduleActivity:
    """A today-schedule invitation the character floated but the user
    has not answered yet."""
    return ScheduleActivity.create(
        start_at=datetime(2026, 7, 26, 15, 0, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 16, 0, tzinfo=UTC),
        description="想約使用者去刨冰店",
        category="social",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="木木",
                role=OPERATOR_INVITE_PENDING_ROLE,
            ),
        ),
    )


async def _run_post_turn(
    response: str,
    *,
    active_schedule: DailySchedule | None = None,
) -> tuple[list, str]:
    model = _ScriptedModel(response)
    processor = LLMPostTurnProcessor(model=model)
    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="好啊，明天早上一起去刨冰店",
        assistant_message="說定了，明天早上見。",
        active_schedule=active_schedule,
        now=datetime(2026, 7, 26, 10, 0, tzinfo=UTC),
    )
    return result.schedule_adjustments, model.prompts[0]


# ------------------------------------------------------------------
# Characterization: why the confirmation must ride on the ``add``
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modify_cannot_reach_a_future_day_activity() -> None:
    """A modify aimed at tomorrow's seed is dropped — it always was.

    ``active_schedule`` is today's, so tomorrow's activity id never
    enters ``known_activity_ids``. This is the structural reason the
    2026-07-26 confirmation never landed, and it is deliberately left
    unchanged: allowing blind cross-day modify would let the model mutate
    activities it cannot see.
    """
    future_seed = ScheduleActivity.create(
        start_at=datetime(2026, 7, 27, 1, 0, tzinfo=UTC),
        end_at=datetime(2026, 7, 27, 3, 0, tzinfo=UTC),
        description="和使用者去刨冰店",
        category="social",
    )
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": ['
        f'{{"action": "modify", "activity_id": "{future_seed.id}", '
        '"operator_involvement": "operator_confirmed_shared"}'
        "]}"
    )

    adjustments, _ = await _run_post_turn(
        response, active_schedule=_today_schedule("c1", [_shared_invite_activity()]),
    )

    assert adjustments == []


# ------------------------------------------------------------------
# Deliverable 1: agreed cross-day activity seeds as confirmed
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agreed_cross_day_activity_lands_confirmed_seed() -> None:
    """The 2026-07-26 case, replayed end to end."""
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": ['
        '{"action": "add", "start": "09:00", "end": "11:00", '
        '"description": "和木木一起去刨冰店", "category": "social", '
        '"operator_involvement": "operator_confirmed_shared", '
        '"operator_display_name": "木木", '
        '"target_date_iso": "2026-07-27"}'
        "]}"
    )

    adjustments, _ = await _run_post_turn(
        response, active_schedule=_today_schedule("c1", [_shared_invite_activity()]),
    )
    assert len(adjustments) == 1
    assert adjustments[0].operator_involvement == OPERATOR_CONFIRMED_SHARED_ROLE
    assert adjustments[0].target_date_iso == "2026-07-27"

    repo = InMemoryScheduleRepository()
    service = _FrozenScheduleService(repository=repo, planner=_UnusedPlanner(), local_tz=UTC)
    await service.apply_adjustments(character_id="c1", adjustments=adjustments)

    seed = await repo.get("c1", _TOMORROW)
    assert seed is not None
    assert seed.is_planned is False
    assert len(seed.activities) == 1
    ref = _operator_ref(seed.activities[0])
    assert ref is not None
    assert ref.role == OPERATOR_CONFIRMED_SHARED_ROLE
    assert ref.display_name == "木木"


@pytest.mark.asyncio
async def test_unanswered_cross_day_invite_still_seeds_pending() -> None:
    """Non-regression: a one-sided invitation must NOT be promoted."""
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": ['
        '{"action": "add", "start": "09:00", "end": "11:00", '
        '"description": "想約使用者去刨冰店", "category": "social", '
        '"operator_involvement": "operator_invite_pending", '
        '"target_date_iso": "2026-07-27"}'
        "]}"
    )

    adjustments, _ = await _run_post_turn(response)
    repo = InMemoryScheduleRepository()
    service = _FrozenScheduleService(repository=repo, planner=_UnusedPlanner(), local_tz=UTC)
    await service.apply_adjustments(character_id="c1", adjustments=adjustments)

    seed = await repo.get("c1", _TOMORROW)
    assert seed is not None
    ref = _operator_ref(seed.activities[0])
    assert ref is not None
    assert ref.role == OPERATOR_INVITE_PENDING_ROLE


# ------------------------------------------------------------------
# Deliverable 1, second half: decline / reschedule must not regress
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decline_downgrades_todays_invite_to_wish() -> None:
    existing = _shared_invite_activity()
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": ['
        f'{{"action": "modify", "activity_id": "{existing.id}", '
        '"operator_involvement": "operator_wish"}'
        "]}"
    )

    adjustments, _ = await _run_post_turn(
        response, active_schedule=_today_schedule("c1", [existing]),
    )
    assert len(adjustments) == 1

    repo = InMemoryScheduleRepository()
    await repo.save(_today_schedule("c1", [existing]))
    service = _FrozenScheduleService(repository=repo, planner=_UnusedPlanner(), local_tz=UTC)
    await service.apply_adjustments(character_id="c1", adjustments=adjustments)

    stored = await repo.get("c1", _TODAY)
    assert stored is not None
    ref = _operator_ref(stored.activities[0])
    assert ref is not None
    assert ref.role == OPERATOR_WISH_ROLE


@pytest.mark.asyncio
async def test_decline_can_still_remove_todays_invite() -> None:
    existing = _shared_invite_activity()
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": ['
        f'{{"action": "remove", "activity_id": "{existing.id}"}}'
        "]}"
    )

    adjustments, _ = await _run_post_turn(
        response, active_schedule=_today_schedule("c1", [existing]),
    )

    repo = InMemoryScheduleRepository()
    await repo.save(_today_schedule("c1", [existing]))
    service = _FrozenScheduleService(repository=repo, planner=_UnusedPlanner(), local_tz=UTC)
    await service.apply_adjustments(character_id="c1", adjustments=adjustments)

    stored = await repo.get("c1", _TODAY)
    assert stored is not None
    assert stored.activities == ()


@pytest.mark.asyncio
async def test_reschedule_seeds_the_new_day_and_clears_the_old_one() -> None:
    """"改成明天早上" — new day gets the confirmed add, today's row loses
    the stale block."""
    existing = _shared_invite_activity()
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": ['
        f'{{"action": "remove", "activity_id": "{existing.id}"}},'
        '{"action": "add", "start": "09:00", "end": "11:00", '
        '"description": "和木木一起去刨冰店", "category": "social", '
        '"operator_involvement": "operator_confirmed_shared", '
        '"target_date_iso": "2026-07-27"}'
        "]}"
    )

    adjustments, _ = await _run_post_turn(
        response, active_schedule=_today_schedule("c1", [existing]),
    )
    assert len(adjustments) == 2

    repo = InMemoryScheduleRepository()
    await repo.save(_today_schedule("c1", [existing]))
    service = _FrozenScheduleService(repository=repo, planner=_UnusedPlanner(), local_tz=UTC)
    await service.apply_adjustments(character_id="c1", adjustments=adjustments)

    today_row = await repo.get("c1", _TODAY)
    assert today_row is not None
    assert today_row.activities == ()

    seed = await repo.get("c1", _TOMORROW)
    assert seed is not None
    ref = _operator_ref(seed.activities[0])
    assert ref is not None
    assert ref.role == OPERATOR_CONFIRMED_SHARED_ROLE


# ------------------------------------------------------------------
# Prompt rules (LLM-first: the behaviour above is only reachable if the
# template actually asks for it)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_states_cross_day_confirmation_rule() -> None:
    _, prompt = await _run_post_turn('{"memories": []}')

    assert "跨日的共同約定" in prompt
    assert "沒有辦法事後用 modify 去把它升級" in prompt
    assert '"operator_confirmed_shared"、target_date_iso' in prompt
    # The judgement stays semantic — who agreed, not who proposed.
    assert "不是「這件事是誰先提的」" in prompt


@pytest.mark.asyncio
async def test_prompt_keeps_decline_and_pending_branches() -> None:
    """The new rule must not swallow the existing downgrade paths."""
    _, prompt = await _run_post_turn('{"memories": []}')

    assert "使用者明確婉拒" in prompt
    assert "operator_wish" in prompt
    assert "還沒回應" in prompt
    assert "operator_invite_pending" in prompt
    # Reschedule branch.
    assert "使用者要改期" in prompt
