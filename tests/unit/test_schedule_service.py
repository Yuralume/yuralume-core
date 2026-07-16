"""ScheduleService tests — focus on lazy generation and current-activity resolution."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)


UTC = timezone.utc


class CountingPlanner:
    """Planner stub that records call count + emits a fixed schedule."""

    def __init__(self) -> None:
        self.calls = 0

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        recent_dialogue_summary: str = "",
        **_: object,
    ) -> DailySchedule:
        self.calls += 1
        activity = ScheduleActivity.create(
            start_at=datetime.combine(date_, datetime.min.time(), tzinfo=local_tz).replace(hour=9),
            end_at=datetime.combine(date_, datetime.min.time(), tzinfo=local_tz).replace(hour=12),
            description=f"call-{self.calls}",
            category="work",
        )
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[activity],
        )


def _character(*, user_id: str = DEFAULT_OPERATOR_ID) -> Character:
    return Character.create(
        name="Mio",
        summary="",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


@pytest.mark.asyncio
async def test_ensure_schedule_generates_once_per_day() -> None:
    repo = InMemoryScheduleRepository()
    planner = CountingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    character = _character()

    first = await service.ensure_schedule(character, date_=date(2026, 4, 18))
    second = await service.ensure_schedule(character, date_=date(2026, 4, 18))

    assert planner.calls == 1
    assert first.id == second.id


@pytest.mark.asyncio
async def test_regenerate_forces_planner_call() -> None:
    repo = InMemoryScheduleRepository()
    planner = CountingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    character = _character()

    await service.ensure_schedule(character, date_=date(2026, 4, 18))
    regenerated = await service.regenerate(character, date_=date(2026, 4, 18))

    assert planner.calls == 2
    assert regenerated.activities[0].description == "call-2"


@pytest.mark.asyncio
async def test_ensure_schedule_passes_initial_relationship_to_planner() -> None:
    class CapturingPlanner(CountingPlanner):
        def __init__(self) -> None:
            super().__init__()
            self.relationship_context = ""
            self.schedule_policy = ""

        async def plan_day(self, **kwargs):  # noqa: ANN003, ANN201
            self.relationship_context = kwargs.get(
                "operator_relationship_context", "",
            )
            self.schedule_policy = kwargs.get(
                "schedule_involvement_policy", "",
            )
            return await super().plan_day(**kwargs)

    repo = InMemoryScheduleRepository()
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    planner = CapturingPlanner()
    service = ScheduleService(
        repository=repo,
        planner=planner,
        local_tz=UTC,
        relationship_seed_repository=relationship_repo,
    )
    character = _character()
    await relationship_repo.save(
        CharacterOperatorRelationshipSeed(
            character_id=character.id,
            operator_id=DEFAULT_OPERATOR_ID,
            relationship_label="朋友",
            known_context="只知道彼此喜歡咖啡，不知道共同往事。",
            living_arrangement="住在使用者家裡。",
            schedule_involvement_policy="mention_only",
        ),
    )

    await service.ensure_schedule(character, date_=date(2026, 4, 18))

    assert "朋友" in planner.relationship_context
    assert "居住安排：住在使用者家裡" in planner.relationship_context
    assert "共同往事" in planner.relationship_context
    assert planner.schedule_policy == "mention_only"


@pytest.mark.asyncio
async def test_ensure_schedule_loads_relationship_for_character_owner() -> None:
    class CapturingPlanner(CountingPlanner):
        def __init__(self) -> None:
            super().__init__()
            self.relationship_context = ""

        async def plan_day(self, **kwargs):  # noqa: ANN003, ANN201
            self.relationship_context = kwargs.get(
                "operator_relationship_context", "",
            )
            return await super().plan_day(**kwargs)

    repo = InMemoryScheduleRepository()
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    planner = CapturingPlanner()
    service = ScheduleService(
        repository=repo,
        planner=planner,
        local_tz=UTC,
        relationship_seed_repository=relationship_repo,
    )
    character = _character(user_id="alice")
    await relationship_repo.save(
        CharacterOperatorRelationshipSeed(
            character_id=character.id,
            operator_id="alice",
            relationship_label="創作搭檔",
            known_context="只知道彼此合作寫故事。",
        ),
    )

    await service.ensure_schedule(character, date_=date(2026, 4, 18))

    assert "創作搭檔" in planner.relationship_context
    assert "合作寫故事" in planner.relationship_context


@pytest.mark.asyncio
async def test_ensure_schedule_swallows_planner_errors() -> None:
    class BrokenPlanner:
        async def plan_day(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("planner exploded")

    repo = InMemoryScheduleRepository()
    service = ScheduleService(repository=repo, planner=BrokenPlanner(), local_tz=UTC)
    character = _character()

    schedule = await service.ensure_schedule(character, date_=date(2026, 4, 18))
    assert schedule.activities == ()


@pytest.mark.asyncio
async def test_resolve_current_returns_matching_activity_and_upcoming() -> None:
    repo = InMemoryScheduleRepository()
    service = ScheduleService(repository=repo, planner=CountingPlanner(), local_tz=UTC)
    character = _character()
    schedule = await service.ensure_schedule(character, date_=date(2026, 4, 18))

    now = datetime(2026, 4, 18, 10, 0, tzinfo=UTC)
    current, upcoming, _just_finished = service.resolve_current(schedule, now=now)
    assert current is not None
    assert current.description.startswith("call-")
    assert upcoming == []


def test_resolve_completed_today_orders_limits_and_excludes_encounters() -> None:
    repo = InMemoryScheduleRepository()
    service = ScheduleService(repository=repo, planner=CountingPlanner(), local_tz=UTC)
    target = date(2026, 4, 18)
    encounter = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 11, 0, tzinfo=UTC),
        description="和B短暫碰面",
        category="social",
        participant_refs=(
            ParticipantRef(
                actor_kind="character",
                actor_id="c2",
                display_name="B",
                role="encounter_partner",
            ),
        ),
    )
    schedule = DailySchedule.create(
        character_id="c1",
        date_=target,
        activities=[
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 17, 23, 0, tzinfo=UTC),
                end_at=datetime(2026, 4, 18, 0, 30, tzinfo=UTC),
                description="跨夜整理",
                category="work",
            ),
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 8, 0, tzinfo=UTC),
                end_at=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
                description="晨間練習",
                category="routine",
            ),
            encounter,
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
                end_at=datetime(2026, 4, 18, 13, 0, tzinfo=UTC),
                description="午間筆記",
                category="work",
            ),
        ],
    )

    completed = service.resolve_completed_today(
        schedule,
        now=datetime(2026, 4, 18, 14, 0, tzinfo=UTC),
        local_tz=UTC,
        limit=2,
    )

    assert [activity.description for activity in completed] == [
        "晨間練習",
        "午間筆記",
    ]


@pytest.mark.asyncio
async def test_current_activity_response_returns_empty_when_no_schedule() -> None:
    repo = InMemoryScheduleRepository()
    service = ScheduleService(repository=repo, planner=CountingPlanner(), local_tz=UTC)
    response = await service.current_activity_response("missing-id")
    assert response.current is None
    assert response.upcoming == []


class _ArcCapturingPlanner:
    """Planner stub that records the today_beat / upcoming_beats it receives."""

    def __init__(self) -> None:
        self.captured_today_beat = None
        self.captured_upcoming: tuple = ()

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        recent_dialogue_summary: str = "",
        today_beat=None,
        upcoming_beats=(),
        **_: object,
    ) -> DailySchedule:
        self.captured_today_beat = today_beat
        self.captured_upcoming = upcoming_beats
        return DailySchedule.create(
            character_id=character.id, date_=date_,
            activities=[
                ScheduleActivity.create(
                    start_at=datetime.combine(date_, datetime.min.time(), tzinfo=local_tz).replace(hour=14),
                    end_at=datetime.combine(date_, datetime.min.time(), tzinfo=local_tz).replace(hour=15),
                    description="x", category="x",
                ),
            ],
        )


@pytest.mark.asyncio
async def test_ensure_schedule_passes_arc_beats_to_planner() -> None:
    """When story_arc_service is wired, ScheduleService must pull today's
    beat + the next upcoming beat and forward them to plan_day so the
    schedule embeds the arc's scenes instead of running parallel."""
    from kokoro_link.domain.entities.story_arc import (
        StoryArc, StoryArcBeat,
    )

    target = date(2026, 4, 18)
    arc_id = "arc-test"
    today_beat = StoryArcBeat.create(
        arc_id=arc_id,
        sequence=1, scheduled_date=target,
        title="公告欄發現試鏡海報", summary="今天她在公告欄看到一張海報",
        tension="rising", scene_type="encounter", location="學校公告欄",
        scene_characters=("室友",), dramatic_question="她敢去試嗎？",
    )
    future_beat = StoryArcBeat.create(
        arc_id=arc_id,
        sequence=2, scheduled_date=target + timedelta(days=2),
        title="試鏡前夜", summary="緊張到睡不著", tension="climax",
    )
    arc = StoryArc.create(
        id=arc_id,
        character_id="c1", title="試鏡前夕", premise="...",
        theme="ambition", start_date=target - timedelta(days=1),
        end_date=target + timedelta(days=10),
        beats=[today_beat, future_beat],
    )

    class _StubArcService:
        async def ensure_active_arc(self, character, *, today=None, auto_start=True):
            assert auto_start is False, "schedule path must not trigger arc planning"
            return arc

    repo = InMemoryScheduleRepository()
    planner = _ArcCapturingPlanner()
    service = ScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
        story_arc_service=_StubArcService(),
    )
    character = _character()
    await service.ensure_schedule(character, date_=target)

    assert planner.captured_today_beat is not None
    assert planner.captured_today_beat.title == "公告欄發現試鏡海報"
    assert planner.captured_today_beat.location == "學校公告欄"
    assert len(planner.captured_upcoming) == 1
    assert planner.captured_upcoming[0].title == "試鏡前夜"


@pytest.mark.asyncio
async def test_ensure_schedule_falls_back_to_next_beat_when_today_empty() -> None:
    """Gap days (no beat scheduled for target) must still get an arc
    anchor — the schedule service promotes the next forward beat into
    the today_beat slot. The promoted beat is removed from the upcoming
    list to avoid double-mention. Planner-side rendering separately
    detects ``scheduled_date != target`` and switches to a "preparation"
    block so the prompt doesn't claim the scene plays today."""
    from kokoro_link.domain.entities.story_arc import (
        StoryArc, StoryArcBeat,
    )

    target = date(2026, 4, 18)
    arc_id = "arc-fallback-test"
    # Today (target) has NO beat — gap day. Next beat is 3 days out,
    # one further beat 5 days out.
    next_beat = StoryArcBeat.create(
        arc_id=arc_id,
        sequence=1, scheduled_date=target + timedelta(days=3),
        title="試鏡前夜", summary="緊張到睡不著",
        tension="climax", scene_type="conflict",
        location="家中",
    )
    later_beat = StoryArcBeat.create(
        arc_id=arc_id,
        sequence=2, scheduled_date=target + timedelta(days=5),
        title="試鏡當天", summary="走上舞台", tension="climax",
    )
    arc = StoryArc.create(
        id=arc_id,
        character_id="c1", title="試鏡前夕", premise="...",
        theme="ambition", start_date=target - timedelta(days=1),
        end_date=target + timedelta(days=10),
        beats=[next_beat, later_beat],
    )

    class _StubArcService:
        async def ensure_active_arc(self, character, *, today=None, auto_start=True):
            return arc

    repo = InMemoryScheduleRepository()
    planner = _ArcCapturingPlanner()
    service = ScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
        story_arc_service=_StubArcService(),
    )
    await service.ensure_schedule(_character(), date_=target)

    # next_beat got promoted into today_beat slot.
    assert planner.captured_today_beat is not None
    assert planner.captured_today_beat.title == "試鏡前夜"
    # Promoted beat must NOT also appear in upcoming.
    assert all(b.title != "試鏡前夜" for b in planner.captured_upcoming)
    # The further-out beat is still surfaced as upcoming.
    assert len(planner.captured_upcoming) == 1
    assert planner.captured_upcoming[0].title == "試鏡當天"


@pytest.mark.asyncio
async def test_ensure_schedule_no_arc_service_passes_none() -> None:
    """Without story_arc_service the planner gets ``today_beat=None``,
    not an exception. Backward-compatible default."""
    repo = InMemoryScheduleRepository()
    planner = _ArcCapturingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    await service.ensure_schedule(_character(), date_=date(2026, 4, 18))
    assert planner.captured_today_beat is None
    assert planner.captured_upcoming == ()


@pytest.mark.asyncio
async def test_ensure_schedule_arc_lookup_failure_is_silent() -> None:
    """Arc lookup is best-effort enrichment; failures must not block
    schedule generation."""

    class _BrokenArcService:
        async def ensure_active_arc(self, character, *, today=None, auto_start=True):
            raise RuntimeError("arc lookup boom")

    repo = InMemoryScheduleRepository()
    planner = _ArcCapturingPlanner()
    service = ScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
        story_arc_service=_BrokenArcService(),
    )
    schedule = await service.ensure_schedule(_character(), date_=date(2026, 4, 18))
    assert len(schedule.activities) == 1  # planner still ran
    assert planner.captured_today_beat is None


@pytest.mark.asyncio
async def test_delete_for_character_removes_schedule() -> None:
    repo = InMemoryScheduleRepository()
    service = ScheduleService(repository=repo, planner=CountingPlanner(), local_tz=UTC)
    character = _character()
    await service.ensure_schedule(character, date_=date(2026, 4, 18))

    removed = await service.delete_for_character(character.id)
    assert removed == 1

    after = await service.get_schedule(character.id, date_=date(2026, 4, 18))
    assert after is None
