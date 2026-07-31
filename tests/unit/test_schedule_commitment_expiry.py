"""CF3 — commitment expiry state machine (schedule sweep + carry-forward).

Covers the "zombie appointment" defect: an invite created on day D for
day D+1 that the user never acted on kept living forever as
``operator_invite_pending``, so every later plan could still treat it as
a live future commitment.

The state machine lives entirely in ``participant_refs`` role values (no
alembic migration): a past-dated ``operator_invite_pending`` becomes
``operator_invite_expired``; a past-dated ``operator_confirmed_shared``
that never produced a memory becomes ``operator_confirmed_lapsed``.
Legacy rows carry neither value and are therefore un-expired *candidates*
that the sweep marks on its next pass.

The first two tests are **characterization**: they pin the carry-forward
behaviour that must survive this change untouched.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.post_turn import ScheduleAdjustment
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_CONFIRMED_LAPSED_ROLE,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_EXPIRED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
    ScheduleActivity,
    expire_operator_commitment,
    has_expired_operator_commitment,
    has_live_operator_commitment,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)


UTC = timezone.utc

# D = invite created, D1 = invite date (never executed), D2 = replanning day.
D = date(2026, 7, 26)
D1 = date(2026, 7, 27)
D2 = date(2026, 7, 29)
NOW_D2 = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)

_INVITE_TEXT = "和使用者一起去先前說好的那家店"


def _character(*, user_id: str = "default") -> Character:
    return Character.create(
        name="Aki",
        summary="",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _operator_ref(role: str) -> ParticipantRef:
    return ParticipantRef(
        actor_kind="operator",
        actor_id="user-1",
        display_name="你",
        role=role,
    )


def _activity(
    *,
    day: date,
    hour: int = 10,
    description: str = _INVITE_TEXT,
    role: str | None = None,
    memorialized: bool = False,
    has_memory: bool = False,
) -> ScheduleActivity:
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=hour)
    refs = (_operator_ref(role),) if role is not None else ()
    return ScheduleActivity.create(
        start_at=start,
        end_at=start + timedelta(hours=2),
        description=description,
        category="social",
        memorialized=memorialized,
        has_memory=has_memory,
        participant_refs=refs,
    )


class _EchoPlanner:
    """Planner stub that emits exactly what it is handed.

    Mirrors the fake-provider planner path (``LLMSchedulePlanner`` returns
    ``pre_committed_activities`` verbatim when the provider is fake), so any
    commitment that leaks into the planner input becomes visible in the
    resulting day — which is what the acceptance sequence asserts against.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        pre_committed_activities: tuple[ScheduleActivity, ...] = (),
        expired_operator_commitments: tuple[ScheduleActivity, ...] = (),
        **_: object,
    ) -> DailySchedule:
        self.calls.append(
            {
                "date": date_,
                "pre_committed": tuple(pre_committed_activities),
                "expired": tuple(expired_operator_commitments),
            },
        )
        base = datetime.combine(date_, datetime.min.time(), tzinfo=local_tz)
        filler = ScheduleActivity.create(
            start_at=base.replace(hour=7),
            end_at=base.replace(hour=8),
            description="早餐",
            category="meal",
        )
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[filler, *pre_committed_activities],
            is_planned=True,
        )

    def call_for(self, target: date) -> dict | None:
        for call in self.calls:
            if call["date"] == target:
                return call
        return None


def _service(repo: InMemoryScheduleRepository, planner: _EchoPlanner) -> ScheduleService:
    return ScheduleService(repository=repo, planner=planner, local_tz=UTC)


def _service_on(
    day: date, repo: InMemoryScheduleRepository, planner: _EchoPlanner,
) -> ScheduleService:
    """A service whose wall-clock civil day is pinned to ``day``.

    Lets one test walk several civil days over a shared repository — the
    only way to reproduce "invited on D, ignored on D+1, replanned on D+2"
    against paths (``apply_adjustments``) that read the clock directly.
    """

    class _Frozen(ScheduleService):
        def today(self, now: datetime | None = None) -> date:
            return day

    return _Frozen(repository=repo, planner=planner, local_tz=UTC)


# --------------------------------------------------------------------- #
# Characterization — carry-forward behaviour that must NOT change.
# --------------------------------------------------------------------- #


def test_characterization_planned_row_carries_only_live_operator_commitments() -> None:
    """A stale-today re-plan carries every *live* operator commitment
    (pending / confirmed / wish) and nothing else. Pinned before the
    expiry sweep landed so the change cannot silently widen or narrow it."""
    schedule = DailySchedule.create(
        character_id="c1",
        date_=D2,
        activities=[
            _activity(day=D2, hour=9, description="獨自散步"),
            _activity(day=D2, hour=11, description="想邀請", role=OPERATOR_INVITE_PENDING_ROLE),
            _activity(day=D2, hour=14, description="已約好", role=OPERATOR_CONFIRMED_SHARED_ROLE),
            _activity(day=D2, hour=17, description="只是想到對方", role=OPERATOR_WISH_ROLE),
        ],
        is_planned=True,
    )

    carried = ScheduleService._carry_forward_commitments(schedule)

    assert [a.description for a in carried] == ["想邀請", "已約好", "只是想到對方"]


def test_characterization_seed_row_carries_every_activity() -> None:
    """A seed-only row (chat-extracted future commitment not yet folded
    into a full day) carries all of its activities, operator-linked or not."""
    schedule = DailySchedule.create(
        character_id="c1",
        date_=D2,
        activities=[
            _activity(day=D2, hour=9, description="獨自散步"),
            _activity(day=D2, hour=19, description="看電影", role=OPERATOR_INVITE_PENDING_ROLE),
        ],
        is_planned=False,
    )

    carried = ScheduleService._carry_forward_commitments(schedule)

    assert [a.description for a in carried] == ["獨自散步", "看電影"]


# --------------------------------------------------------------------- #
# Domain — the role transition itself.
# --------------------------------------------------------------------- #


def test_pending_invite_transitions_to_expired() -> None:
    activity = _activity(day=D1, role=OPERATOR_INVITE_PENDING_ROLE)

    swept = expire_operator_commitment(activity)

    assert swept is not None
    assert [ref.role for ref in swept.participant_refs] == [
        OPERATOR_INVITE_EXPIRED_ROLE,
    ]
    assert swept.description == activity.description, "使用者可見文本不得被改寫"
    assert swept.id == activity.id


def test_unmemorialized_confirmed_shared_transitions_to_lapsed() -> None:
    activity = _activity(day=D1, role=OPERATOR_CONFIRMED_SHARED_ROLE)

    swept = expire_operator_commitment(activity)

    assert swept is not None
    assert [ref.role for ref in swept.participant_refs] == [
        OPERATOR_CONFIRMED_LAPSED_ROLE,
    ]


def test_memorialized_confirmed_shared_with_a_memory_is_left_alone() -> None:
    """A shared activity that produced a memory actually happened — it is
    history, not a lapsed promise."""
    activity = _activity(
        day=D1,
        role=OPERATOR_CONFIRMED_SHARED_ROLE,
        memorialized=True,
        has_memory=True,
    )

    assert expire_operator_commitment(activity) is None


def test_memorialized_without_memory_confirmed_shared_still_lapses() -> None:
    """CF3×CF4 seam: ``memorialized`` is the memorialiser's idempotency
    latch, and since CF4 it gets set even when the run deliberately writes
    no memory (a shared plan with no evidence the operator took part).
    Only ``has_memory`` proves history, so a latched-but-memory-less block
    must still lapse — otherwise the unattended appointment stays a live
    commitment forever and never reaches the expired-facts list."""
    activity = _activity(
        day=D1,
        role=OPERATOR_CONFIRMED_SHARED_ROLE,
        memorialized=True,
        has_memory=False,
    )

    swept = expire_operator_commitment(activity)

    assert swept is not None
    assert [ref.role for ref in swept.participant_refs] == [
        OPERATOR_CONFIRMED_LAPSED_ROLE,
    ]
    assert has_expired_operator_commitment(swept) is True
    assert has_live_operator_commitment(swept) is False


def test_wish_and_plain_activities_are_untouched() -> None:
    assert expire_operator_commitment(_activity(day=D1, role=OPERATOR_WISH_ROLE)) is None
    assert expire_operator_commitment(_activity(day=D1)) is None


def test_legacy_rows_without_new_roles_are_expiry_candidates() -> None:
    """Backward compatibility: rows written before this change carry no
    expired role value, so they read as un-expired and stay eligible for
    the sweep to mark on its next pass."""
    legacy = _activity(day=D1, role=OPERATOR_INVITE_PENDING_ROLE)

    assert has_expired_operator_commitment(legacy) is False
    assert has_live_operator_commitment(legacy) is True

    swept = expire_operator_commitment(legacy)

    assert swept is not None
    assert has_expired_operator_commitment(swept) is True
    assert has_live_operator_commitment(swept) is False
    # Idempotent: a second pass finds nothing left to change.
    assert expire_operator_commitment(swept) is None


# --------------------------------------------------------------------- #
# Carry-forward exclusion.
# --------------------------------------------------------------------- #


def test_expired_activities_are_dropped_from_carry_forward() -> None:
    planned = DailySchedule.create(
        character_id="c1",
        date_=D2,
        activities=[
            _activity(day=D2, hour=11, description="過期邀請", role=OPERATOR_INVITE_EXPIRED_ROLE),
            _activity(day=D2, hour=14, description="失效約定", role=OPERATOR_CONFIRMED_LAPSED_ROLE),
            _activity(day=D2, hour=17, description="仍在邀請中", role=OPERATOR_INVITE_PENDING_ROLE),
        ],
        is_planned=True,
    )
    seed = DailySchedule.create(
        character_id="c1",
        date_=D2,
        activities=[
            _activity(day=D2, hour=11, description="過期邀請", role=OPERATOR_INVITE_EXPIRED_ROLE),
            _activity(day=D2, hour=17, description="獨自散步"),
        ],
        is_planned=False,
    )

    assert [
        a.description for a in ScheduleService._carry_forward_commitments(planned)
    ] == ["仍在邀請中"]
    assert [
        a.description for a in ScheduleService._carry_forward_commitments(seed)
    ] == ["獨自散步"]


# --------------------------------------------------------------------- #
# Service — the sweep on the ensure / plan path.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_plan_path_sweeps_and_persists_past_pending_invite() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        DailySchedule.create(
            character_id=character.id,
            date_=D1,
            activities=[_activity(day=D1, role=OPERATOR_INVITE_PENDING_ROLE)],
            is_planned=True,
        ),
    )
    planner = _EchoPlanner()

    await _service(repo, planner).ensure_schedule(character, date_=D2, now=NOW_D2)

    stored = await repo.get(character.id, D1)
    assert stored is not None
    assert [ref.role for ref in stored.activities[0].participant_refs] == [
        OPERATOR_INVITE_EXPIRED_ROLE,
    ]


@pytest.mark.asyncio
async def test_sweep_does_not_touch_todays_or_future_commitments() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    for target in (D2, D2 + timedelta(days=1)):
        await repo.save(
            DailySchedule.create(
                character_id=character.id,
                date_=target,
                activities=[_activity(day=target, role=OPERATOR_INVITE_PENDING_ROLE)],
                is_planned=False,
            ),
        )
    planner = _EchoPlanner()

    await _service(repo, planner).ensure_schedule(character, date_=D2, now=NOW_D2)

    tomorrow = await repo.get(character.id, D2 + timedelta(days=1))
    assert tomorrow is not None
    assert [ref.role for ref in tomorrow.activities[0].participant_refs] == [
        OPERATOR_INVITE_PENDING_ROLE,
    ]


@pytest.mark.asyncio
async def test_expired_invites_reach_the_planner_as_a_fact_list() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        DailySchedule.create(
            character_id=character.id,
            date_=D1,
            activities=[_activity(day=D1, role=OPERATOR_INVITE_PENDING_ROLE)],
            is_planned=True,
        ),
    )
    planner = _EchoPlanner()

    await _service(repo, planner).ensure_schedule(character, date_=D2, now=NOW_D2)

    call = planner.call_for(D2)
    assert call is not None
    assert [a.description for a in call["expired"]] == [_INVITE_TEXT]
    assert call["pre_committed"] == ()


@pytest.mark.asyncio
async def test_unattended_confirmed_shared_reaches_planner_after_memorializer_latch() -> None:
    """The CF3×CF4 seam end-to-end: the memorialiser latched the block
    (``memorialized=True``) but wrote no memory because nothing showed the
    operator was there. The sweep must still retire it, so the planner is
    told never to reschedule it."""
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        DailySchedule.create(
            character_id=character.id,
            date_=D1,
            activities=[
                _activity(
                    day=D1,
                    role=OPERATOR_CONFIRMED_SHARED_ROLE,
                    memorialized=True,
                    has_memory=False,
                ),
            ],
            is_planned=True,
        ),
    )
    planner = _EchoPlanner()

    await _service(repo, planner).ensure_schedule(character, date_=D2, now=NOW_D2)

    stored = await repo.get(character.id, D1)
    assert stored is not None
    assert [ref.role for ref in stored.activities[0].participant_refs] == [
        OPERATOR_CONFIRMED_LAPSED_ROLE,
    ]
    call = planner.call_for(D2)
    assert call is not None
    assert [a.description for a in call["expired"]] == [_INVITE_TEXT]


@pytest.mark.asyncio
async def test_past_seed_row_pending_invite_is_not_carried_forward() -> None:
    """Planning a past day whose seed invite never happened must not
    re-materialise the invite as a live commitment."""
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        DailySchedule.create(
            character_id=character.id,
            date_=D1,
            activities=[_activity(day=D1, role=OPERATOR_INVITE_PENDING_ROLE)],
            is_planned=False,
        ),
    )
    planner = _EchoPlanner()

    result = await _service(repo, planner).ensure_schedule(
        character, date_=D1, now=NOW_D2,
    )

    call = planner.call_for(D1)
    assert call is not None
    assert call["pre_committed"] == ()
    assert [a.description for a in call["expired"]] == [_INVITE_TEXT]
    assert _INVITE_TEXT not in {a.description for a in result.activities}


@pytest.mark.asyncio
async def test_regenerate_also_sweeps_and_excludes_expired() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        DailySchedule.create(
            character_id=character.id,
            date_=D1,
            activities=[_activity(day=D1, role=OPERATOR_INVITE_PENDING_ROLE)],
            is_planned=False,
        ),
    )
    planner = _EchoPlanner()

    await _service_on(D2, repo, planner).regenerate(character, date_=D1)

    call = planner.call_for(D1)
    assert call is not None
    assert call["pre_committed"] == ()
    assert [a.description for a in call["expired"]] == [_INVITE_TEXT]


# --------------------------------------------------------------------- #
# Acceptance — D 建立邀請 / D+1 未執行 / D+2 規劃.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invite_created_on_d_never_executed_on_d1_is_absent_on_d2() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    planner = _EchoPlanner()

    # Day D: chat extracts "明天一起去…" → future seed row on D+1.
    await _service_on(D, repo, planner).apply_adjustments(
        character_id=character.id,
        date_=D,
        adjustments=[
            ScheduleAdjustment(
                action="add",
                start="10:00",
                end="12:00",
                description=_INVITE_TEXT,
                category="social",
                target_date_iso=D1.isoformat(),
                operator_involvement=OPERATOR_INVITE_PENDING_ROLE,
            ),
        ],
    )
    seeded = await repo.get(character.id, D1)
    assert seeded is not None and len(seeded.activities) == 1

    # Day D+1 passes with the invite never confirmed and never executed.

    # Day D+2: the rolling-window pre-planner runs.
    window = await _service_on(D2, repo, planner).ensure_window(
        character, now=NOW_D2,
    )

    planned_d2 = next(s for s in window if s.date == D2)
    assert _INVITE_TEXT not in {a.description for a in planned_d2.activities}
    assert not any(
        has_live_operator_commitment(a) for a in planned_d2.activities
    ), "殭屍邀請不得以任何活躍狀態重新出現在 D+2"

    call = planner.call_for(D2)
    assert call is not None
    assert call["pre_committed"] == ()
    assert [a.description for a in call["expired"]] == [_INVITE_TEXT]

    swept = await repo.get(character.id, D1)
    assert swept is not None
    assert [ref.role for ref in swept.activities[0].participant_refs] == [
        OPERATOR_INVITE_EXPIRED_ROLE,
    ]
