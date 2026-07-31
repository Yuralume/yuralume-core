"""CF4 regression: the 7/27 刨冰 case must not become a shared memory.

Real incident (測試站, 芊璃): the day's schedule carried "與木木一起前往先前
約定的刨冰店，吃刨冰" with the operator attached as
``operator_invite_pending`` — the user never accepted and never went. The
world simulation memorialised it anyway, the wall posted "和木木一起跑到那
間說好要來的刨冰店，**終於吃到啦**～", and days later the character dug that
"shared memory" up in a proactive message. A fabricated joint experience
had entered long-term memory.

What is testable here is the *contract*, not the model's prose: the
planner's description is never allowed to become the memory body for an
operator-involved activity, the aftermath model receives the evidence it
needs to write an honest line, and that line is what gets stored.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.services.schedule_memorializer import (
    ScheduleMemorializer,
)
from kokoro_link.contracts.activity_aftermath import ActivityAftermath
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_EXPIRED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.domain.services.shared_activity_evidence import (
    OperatorPresenceEvidence,
    SharedActivityEvidence,
    SharedClaimPolicy,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc

_DAY = date(2026, 7, 27)
_START = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
_END = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
_NOW = datetime(2026, 7, 27, 18, 0, tzinfo=UTC)
_PLANNED_DESCRIPTION = "與木木一起前往先前約定的刨冰店，吃刨冰"


class _RecordingAftermathPort:
    """Captures the evidence handed to the model and replays a canned answer."""

    def __init__(self, answer: ActivityAftermath | None = None) -> None:
        self.answer = answer or ActivityAftermath()
        self.seen: list[SharedActivityEvidence | None] = []

    async def judge(
        self,
        *,
        character: Character,
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
        evidence: SharedActivityEvidence | None = None,
    ) -> ActivityAftermath:
        _ = character, activity, operator_primary_language
        self.seen.append(evidence)
        return self.answer


def _character(last_active_at: datetime | None) -> Character:
    character = Character.create(
        name="芊璃",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="平靜",
            affection=60,
            fatigue=20,
            trust=60,
            energy=80,
            last_active_at=last_active_at,
        ),
    )
    return replace(character, user_id="owner-1")


def _activity(role: str) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=_START,
        end_at=_END,
        description=_PLANNED_DESCRIPTION,
        category="social",
        location="刨冰店",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="木木",
                role=role,
            ),
        ),
    )


async def _run(
    *,
    role: str,
    last_active_at: datetime | None,
    port: _RecordingAftermathPort | None,
) -> tuple[int, InMemoryMemoryRepository, InMemoryScheduleRepository, Character]:
    schedules = InMemoryScheduleRepository()
    memories = InMemoryMemoryRepository()
    characters = InMemoryCharacterRepository()
    character = _character(last_active_at)
    await characters.save(character)
    await schedules.save(
        DailySchedule.create(
            character_id=character.id, date_=_DAY, activities=[_activity(role)],
        ),
    )
    memorializer = ScheduleMemorializer(
        schedule_repository=schedules,
        memory_repository=memories,
        local_tz=UTC,
        aftermath_port=port,
        character_repository=characters,
    )
    count = await memorializer.memorialize(character_id=character.id, now=_NOW)
    return count, memories, schedules, character


@pytest.mark.asyncio
async def test_pending_invite_with_absent_user_writes_no_shared_memory() -> None:
    """The incident itself: pending invite, user silent since yesterday,
    no aftermath account available → nothing is written. The planned
    description must never be the fallback body."""
    count, memories, schedules, character = await _run(
        role=OPERATOR_INVITE_PENDING_ROLE,
        last_active_at=datetime(2026, 7, 26, 21, 0, tzinfo=UTC),
        port=_RecordingAftermathPort(),
    )

    assert count == 0
    assert await memories.query(character.id, limit=10) == []
    reloaded = await schedules.get(character.id, _DAY)
    assert reloaded is not None
    # Still marked so the next tick doesn't re-judge it forever.
    assert reloaded.activities[0].memorialized is True
    assert reloaded.activities[0].has_memory is False


@pytest.mark.asyncio
async def test_pending_invite_evidence_reaches_the_aftermath_model() -> None:
    """The discipline is enforced by prompt + facts, so the facts have to
    actually arrive — including the distinction between "he was absent"
    and "we can't tell"."""
    port = _RecordingAftermathPort()
    await _run(
        role=OPERATOR_INVITE_PENDING_ROLE,
        last_active_at=datetime(2026, 7, 26, 21, 0, tzinfo=UTC),
        port=port,
    )

    assert len(port.seen) == 1
    evidence = port.seen[0]
    assert evidence is not None
    assert evidence.policy is SharedClaimPolicy.SOLO_ONLY
    assert evidence.presence is OperatorPresenceEvidence.BEFORE_ACTIVITY
    assert evidence.operator_display_name == "木木"


@pytest.mark.asyncio
async def test_honest_account_replaces_the_planned_description() -> None:
    """When the model answers, its line becomes the memory body — the
    unproven "一起…" plan text does not survive into memory, and the
    absolute date anchor still wraps it."""
    port = _RecordingAftermathPort(ActivityAftermath(
        residue_summary="有點悶",
        emotion_tag="失落",
        factual_summary="本來想約木木一起去那家刨冰店，他沒回我，最後我自己去吃了",
    ))
    count, memories, _, character = await _run(
        role=OPERATOR_INVITE_PENDING_ROLE,
        last_active_at=datetime(2026, 7, 26, 21, 0, tzinfo=UTC),
        port=port,
    )

    assert count == 1
    items = await memories.query(character.id, limit=10)
    assert len(items) == 1
    content = items[0].content
    assert "最後我自己去吃了" in content
    assert _PLANNED_DESCRIPTION not in content
    assert "2026-07-27" in content


@pytest.mark.asyncio
async def test_expired_invite_gets_an_honest_closing_memory() -> None:
    """P4c: a swept-expired invite is not silently evaporated — it becomes
    a truthful memory, which the ordinary recall path can surface later
    ("那天最後自己去了，改天再約？") without any dispatcher change."""
    port = _RecordingAftermathPort(ActivityAftermath(
        factual_summary="那天說好要一起去的刨冰店，等不到他，我自己去了",
    ))
    count, memories, schedules, character = await _run(
        role=OPERATOR_INVITE_EXPIRED_ROLE,
        last_active_at=None,
        port=port,
    )

    assert count == 1
    items = await memories.query(character.id, limit=10)
    assert "我自己去了" in items[0].content
    assert port.seen[0] is not None
    assert port.seen[0].presence is OperatorPresenceEvidence.NEVER
    reloaded = await schedules.get(character.id, _DAY)
    assert reloaded is not None
    assert reloaded.activities[0].has_memory is True


@pytest.mark.asyncio
async def test_confirmed_without_evidence_is_not_written_as_shared() -> None:
    """Agreement is not attendance: the user said yes on 7/26 and then went
    quiet, so the plan text must not become a "we did it" memory."""
    count, memories, _, character = await _run(
        role=OPERATOR_CONFIRMED_SHARED_ROLE,
        last_active_at=datetime(2026, 7, 26, 21, 0, tzinfo=UTC),
        port=_RecordingAftermathPort(),
    )

    assert count == 0
    assert await memories.query(character.id, limit=10) == []


@pytest.mark.asyncio
async def test_confirmed_with_interaction_evidence_keeps_the_plain_path() -> None:
    """Evidence present → the pre-CF4 behaviour is intact: the activity is
    memorialised (description body included) and the operator participant
    rides along on the memory."""
    count, memories, schedules, character = await _run(
        role=OPERATOR_CONFIRMED_SHARED_ROLE,
        last_active_at=datetime(2026, 7, 27, 14, 30, tzinfo=UTC),
        port=_RecordingAftermathPort(),
    )

    assert count == 1
    items = await memories.query(character.id, limit=10)
    assert _PLANNED_DESCRIPTION in items[0].content
    assert items[0].participants[0].role == OPERATOR_CONFIRMED_SHARED_ROLE
    reloaded = await schedules.get(character.id, _DAY)
    assert reloaded is not None
    assert reloaded.activities[0].has_memory is True


@pytest.mark.asyncio
async def test_solo_activity_is_untouched_by_the_new_gate() -> None:
    """No operator ref → no evidence block, no extra requirement. Ordinary
    schedule history must keep flowing exactly as before."""
    schedules = InMemoryScheduleRepository()
    memories = InMemoryMemoryRepository()
    characters = InMemoryCharacterRepository()
    character = _character(None)
    await characters.save(character)
    solo = ScheduleActivity.create(
        start_at=_START,
        end_at=_END,
        description="自己去咖啡店寫稿",
        category="work",
    )
    await schedules.save(
        DailySchedule.create(
            character_id=character.id, date_=_DAY, activities=[solo],
        ),
    )
    port = _RecordingAftermathPort()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedules,
        memory_repository=memories,
        local_tz=UTC,
        aftermath_port=port,
        character_repository=characters,
    )

    count = await memorializer.memorialize(character_id=character.id, now=_NOW)

    assert count == 1
    items = await memories.query(character.id, limit=10)
    assert "自己去咖啡店寫稿" in items[0].content
    assert port.seen == [SharedActivityEvidence()]
