"""ScheduleWeatherDriftService — intra-day vetting of the planned day.

The day is planned once against the weather that happened to be true at
planning time; by afternoon the sky may have turned. This service picks
the blocks that are still ahead, hands them to the LLM judge together
with the weather facts that are true *right now*, and applies whatever
partial rewrites come back.

Per the project's top directive the tests here exercise the *contract* —
which blocks reach the judge, when the cost gate re-opens, how a partial
rewrite is merged — never weather-category rules. The stub judge simply
returns whatever rewrites the test stages, which is enough to prove the
wiring; deciding "河堤散步 under a downpour contradicts, 看電影 doesn't"
belongs to the model.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.services.schedule_weather_drift_service import (
    ScheduleWeatherDriftService,
)
from kokoro_link.contracts.schedule_weather_drift import ActivityWeatherRewrite
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    MeetingAffordance,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
    ScenePrivacy,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc
DAY = date(2026, 7, 28)
NOW = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)

RAIN_BLOCK = "台北目前天氣（事實層）：\n- 現在：大雨，氣溫 24.0°C"
SUNNY_BLOCK = "台北目前天氣（事實層）：\n- 現在：晴朗，氣溫 31.0°C"


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="高中二年級的甜點愛好者",
        personality=["怕生"],
        interests=["甜點"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=20, trust=50, energy=80,
        ),
    )


def _activity(
    start_h: int,
    end_h: int,
    description: str,
    *,
    category: str = "休閒",
    location: str | None = "河堤",
    busy: float = 0.3,
    memorialized: bool = False,
    participant_refs: tuple[ParticipantRef, ...] = (),
    scene_privacy: ScenePrivacy | None = None,
    meeting_affordance: MeetingAffordance | None = None,
) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(DAY.year, DAY.month, DAY.day, start_h, 0, tzinfo=UTC),
        end_at=datetime(DAY.year, DAY.month, DAY.day, end_h, 0, tzinfo=UTC),
        description=description,
        category=category,
        location=location,
        busy_score=busy,
        memorialized=memorialized,
        participant_refs=participant_refs,
        scene_privacy=scene_privacy,
        meeting_affordance=meeting_affordance,
    )


def _operator_ref(role: str) -> ParticipantRef:
    return ParticipantRef(
        actor_kind="operator",
        actor_id="default",
        display_name="玩家",
        role=role,
    )


def _operator(*, language: str = "zh-TW") -> OperatorProfile:
    return OperatorProfile(
        id="default",
        display_name="玩家",
        primary_language=language,
        timezone_id="UTC",
        country_code="TW",
        latitude=25.03,
        longitude=121.56,
        location_label="台北",
    )


class _StubOperatorProfileService:
    def __init__(self, operator: OperatorProfile | None) -> None:
        self._operator = operator
        self.calls: list[str] = []

    async def get_for_user(self, user_id: str) -> OperatorProfile | None:
        self.calls.append(user_id)
        return self._operator


class _StubWeather:
    """Weather port returning a staged block, recording the location used."""

    def __init__(self, block: str) -> None:
        self.block = block
        self.locations: list[object] = []

    async def describe(self, *, now=None, location=None) -> str:  # noqa: ANN001
        self.locations.append(location)
        return self.block


class _StubJudge:
    """Judge returning staged rewrites, recording every window it saw."""

    def __init__(
        self,
        rewrites: tuple[ActivityWeatherRewrite, ...] = (),
        *,
        crash: bool = False,
        during=None,  # noqa: ANN001 — async hook simulating a concurrent writer
    ) -> None:
        self._rewrites = rewrites
        self._crash = crash
        self._during = during
        self.windows: list[tuple[str, ...]] = []
        self.contexts: list[str] = []
        self.languages: list[str] = []

    async def judge(
        self,
        *,
        character,  # noqa: ANN001
        activities,  # noqa: ANN001
        weather_context: str,
        now: datetime,
        local_tz,  # noqa: ANN001
        operator_primary_language: str = "zh-TW",
    ) -> tuple[ActivityWeatherRewrite, ...]:
        self.windows.append(tuple(a.description for a in activities))
        self.contexts.append(weather_context)
        self.languages.append(operator_primary_language)
        if self._during is not None:
            # Whatever else landed on this day while the model was thinking.
            await self._during()
        if self._crash:
            raise RuntimeError("judge down")
        return self._rewrites


class _CountingScheduleRepository(InMemoryScheduleRepository):
    """In-memory repository counting whole-day saves vs marker-only stamps."""

    def __init__(self) -> None:
        super().__init__()
        self.saves = 0
        self.vet_stamps = 0

    async def save(self, schedule: DailySchedule) -> None:
        self.saves += 1
        await super().save(schedule)

    async def set_weather_vet(
        self, character_id, date_, *, activity_id, condition,  # noqa: ANN001
    ) -> None:
        self.vet_stamps += 1
        await super().set_weather_vet(
            character_id, date_, activity_id=activity_id, condition=condition,
        )


def _service(
    *,
    repository: InMemoryScheduleRepository,
    judge: _StubJudge,
    weather: _StubWeather | None,
    operator: OperatorProfile | None = None,
) -> ScheduleWeatherDriftService:
    return ScheduleWeatherDriftService(
        schedule_repository=repository,
        drift_port=judge,
        local_tz=UTC,
        weather_context_port=weather,
        operator_profile_service=_StubOperatorProfileService(
            operator if operator is not None else _operator(),
        ),
    )


async def _seed(
    repository: InMemoryScheduleRepository,
    character: Character,
    activities: list[ScheduleActivity],
    *,
    is_planned: bool = True,
) -> DailySchedule:
    schedule = DailySchedule.create(
        character_id=character.id,
        date_=DAY,
        activities=activities,
        is_planned=is_planned,
    )
    await repository.save(schedule)
    return schedule


# ---------------------------------------------------------------------- #
# Window selection
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_window_is_current_block_plus_next_two() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character,
        [
            _activity(8, 9, "晨跑"),
            _activity(13, 14, "河堤散步"),
            _activity(14, 15, "咖啡廳讀書"),
            _activity(15, 16, "看電影"),
            _activity(16, 17, "回家煮飯"),
        ],
    )
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=NOW)

    # Finished blocks are never rewritten (they already happened), and the
    # window stops after two follow-ups.
    assert judge.windows == [("河堤散步", "咖啡廳讀書", "看電影")]


@pytest.mark.asyncio
async def test_window_starts_at_next_block_when_idle() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character,
        [
            _activity(8, 9, "晨跑"),
            _activity(14, 15, "咖啡廳讀書"),
            _activity(15, 16, "看電影"),
        ],
    )
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=NOW)

    assert judge.windows == [("咖啡廳讀書", "看電影")]


@pytest.mark.asyncio
async def test_protected_blocks_are_excluded_from_the_window() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character,
        [
            _activity(13, 14, "河堤散步"),
            _activity(
                14, 15, "和 Yuki 在車站碰面",
                participant_refs=(
                    ParticipantRef(
                        actor_kind="character",
                        actor_id="peer-1",
                        display_name="Yuki",
                        role="encounter_partner",
                    ),
                ),
            ),
            _activity(
                15, 16, "和你一起去看電影",
                participant_refs=(_operator_ref(OPERATOR_CONFIRMED_SHARED_ROLE),),
            ),
            _activity(16, 17, "回家煮飯", memorialized=True),
            _activity(17, 18, "夜跑"),
        ],
    )
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=NOW)

    # A real meeting with another character, a plan the user was told about,
    # and an already-memorialised block never get silently rewritten.
    assert judge.windows == [("河堤散步", "夜跑")]


@pytest.mark.asyncio
async def test_every_block_the_player_was_told_about_is_excluded() -> None:
    # A pending invite is an invitation the character already spoke aloud and
    # that resolve_pending_invites keeps surfacing until answered; a wish is a
    # plan she voiced wanting. Rewording either behind the player's back is the
    # same breach the confirmed-shared exclusion exists to prevent.
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character,
        [
            _activity(
                13, 14, "問你要不要一起去河堤走走",
                participant_refs=(_operator_ref(OPERATOR_INVITE_PENDING_ROLE),),
            ),
            _activity(
                14, 15, "想找你一起去看夕陽",
                participant_refs=(_operator_ref(OPERATOR_WISH_ROLE),),
            ),
            _activity(15, 16, "一個人去買菜"),
        ],
    )
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=NOW)

    assert judge.windows == [("一個人去買菜",)]


@pytest.mark.asyncio
async def test_window_stops_at_the_look_ahead_horizon() -> None:
    # 09:30 under 大雨 must not re-weld this morning's sky into a 20:00 block:
    # by the time it arrives the weather has moved on, and the gate would have
    # stamped it as vetted so it never gets re-judged.
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character,
        [
            _activity(8, 9, "晨跑"),
            _activity(20, 22, "陽台看星星"),
        ],
    )
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    morning = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)
    assert await service.vet(character, now=morning) is None
    assert judge.windows == []
    # Nothing stamped either, so the block gets a fresh verdict when it nears.
    stored = await repo.get(character.id, DAY)
    assert stored.weather_vet_activity_id is None


@pytest.mark.asyncio
async def test_a_block_inside_the_horizon_is_still_reached_when_idle() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character,
        [_activity(8, 9, "晨跑"), _activity(12, 13, "午餐後散步")],
    )
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=datetime(2026, 7, 28, 9, 30, tzinfo=UTC))

    assert judge.windows == [("午餐後散步",)]


@pytest.mark.asyncio
async def test_no_window_skips_the_judge() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(8, 9, "晨跑")])
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    assert await service.vet(character, now=NOW) is None
    assert judge.windows == []
    assert repo.saves == 1  # only the seed write


@pytest.mark.asyncio
async def test_seed_only_day_is_not_vetted() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character, [_activity(13, 14, "河堤散步")], is_planned=False,
    )
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    assert await service.vet(character, now=NOW) is None
    assert judge.windows == []


@pytest.mark.asyncio
async def test_missing_day_is_a_no_op() -> None:
    repo = _CountingScheduleRepository()
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    assert await service.vet(_character(), now=NOW) is None
    assert judge.windows == []
    assert repo.saves == 0


# ---------------------------------------------------------------------- #
# Weather availability + cost gate
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_weather_block_skips_the_judge() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(""))

    assert await service.vet(character, now=NOW) is None
    assert judge.windows == []
    assert repo.saves == 1  # only the seed write


@pytest.mark.asyncio
async def test_unwired_weather_port_skips_the_judge() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=None)

    assert await service.vet(character, now=NOW) is None
    assert judge.windows == []


@pytest.mark.asyncio
async def test_gate_skips_a_second_pass_over_the_same_block_and_sky() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character,
        [_activity(13, 14, "河堤散步"), _activity(14, 15, "咖啡廳讀書")],
    )
    judge = _StubJudge()
    weather = _StubWeather(RAIN_BLOCK)
    service = _service(repository=repo, judge=judge, weather=weather)

    await service.vet(character, now=NOW)
    saves_after_first = repo.saves
    await service.vet(character, now=NOW.replace(minute=45))

    assert len(judge.windows) == 1
    assert repo.saves == saves_after_first  # nothing to persist on a skip


@pytest.mark.asyncio
async def test_gate_reopens_when_the_sky_turns_mid_block() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])
    judge = _StubJudge()
    weather = _StubWeather(RAIN_BLOCK)
    service = _service(repository=repo, judge=judge, weather=weather)

    await service.vet(character, now=NOW)
    weather.block = SUNNY_BLOCK
    await service.vet(character, now=NOW.replace(minute=45))

    assert judge.contexts == [RAIN_BLOCK, SUNNY_BLOCK]


@pytest.mark.asyncio
async def test_gate_reopens_when_the_day_moves_to_the_next_block() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(
        repo, character,
        [_activity(13, 14, "河堤散步"), _activity(14, 15, "咖啡廳讀書")],
    )
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=NOW)
    await service.vet(character, now=datetime(2026, 7, 28, 14, 10, tzinfo=UTC))

    assert judge.windows == [("河堤散步", "咖啡廳讀書"), ("咖啡廳讀書",)]


@pytest.mark.asyncio
async def test_marker_is_stamped_even_when_nothing_is_rewritten() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])
    judge = _StubJudge()
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    saves_before = repo.saves
    vetted = await service.vet(character, now=NOW)

    assert vetted is not None
    assert vetted.weather_vet_activity_id == vetted.activities[0].id
    assert vetted.weather_vet_condition == "大雨"
    stored = await repo.get(character.id, DAY)
    assert stored.weather_vet_condition == "大雨"
    # "We asked, nothing contradicted" touches only the two marker columns —
    # rewriting the whole day here is what used to revert concurrent writers.
    assert repo.saves == saves_before
    assert repo.vet_stamps == 1


@pytest.mark.asyncio
async def test_gate_reopens_when_the_block_carries_no_condition_phrase() -> None:
    # A forecast-only block yields no "現在" phrase, so there is nothing to
    # compare — re-judge rather than treating "unknown" as "already seen".
    forecast_only = "台北目前天氣（事實層）：\n- 今日溫度：高溫 31.0°C、低溫 26.0°C"
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])
    judge = _StubJudge()
    service = _service(
        repository=repo, judge=judge, weather=_StubWeather(forecast_only),
    )

    await service.vet(character, now=NOW)
    await service.vet(character, now=NOW.replace(minute=45))

    assert len(judge.windows) == 2


# ---------------------------------------------------------------------- #
# Rewrite application
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rewrite_overwrites_only_the_fields_the_judge_filled_in() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    walk = _activity(13, 14, "沿著河堤散步", category="休閒", busy=0.3)
    reading = _activity(14, 15, "咖啡廳讀書", category="興趣", busy=0.4)
    await _seed(repo, character, [walk, reading])
    judge = _StubJudge(
        (
            ActivityWeatherRewrite(
                activity_id=walk.id,
                description="雨太大，改在家裡窗邊看雨寫日記",
                location="家",
                busy_score=0.2,
            ),
            # A judge that says "keep" simply omits the block; an all-default
            # patch must still leave everything alone.
            ActivityWeatherRewrite(activity_id=reading.id),
            ActivityWeatherRewrite(activity_id="not-in-this-day", description="?"),
        ),
    )
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    saves_before = repo.saves
    vetted = await service.vet(character, now=NOW)

    assert repo.saves == saves_before + 1
    assert vetted is not None
    assert len(vetted.activities) == 2
    rewritten, untouched = vetted.activities
    assert rewritten.description == "雨太大，改在家裡窗邊看雨寫日記"
    assert rewritten.location == "家"
    assert rewritten.busy_score == pytest.approx(0.2)
    # Untouched fields keep the planner's values — including the time
    # skeleton, which a weather rewrite may never move.
    assert rewritten.category == "休閒"
    assert rewritten.start_at == walk.start_at
    assert rewritten.end_at == walk.end_at
    assert rewritten.id == walk.id
    assert untouched == reading


@pytest.mark.asyncio
async def test_rewrites_outside_the_window_are_dropped() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    past = _activity(8, 9, "晨跑")
    walk = _activity(13, 14, "河堤散步")
    await _seed(repo, character, [past, walk])
    judge = _StubJudge(
        (ActivityWeatherRewrite(activity_id=past.id, description="改成室內跑步機"),),
    )
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    vetted = await service.vet(character, now=NOW)

    assert vetted is not None
    assert vetted.activities[0].description == "晨跑"


@pytest.mark.asyncio
async def test_rewriting_the_setting_clears_the_stale_scene_verdict() -> None:
    # Scene Access reads scene_privacy / meeting_affordance straight off the
    # activity. Moving 露天座位 indoors while leaving the planner's outdoor
    # verdict behind would keep judging the block by a setting that is gone;
    # clearing them lets that judge re-infer from the new prose.
    repo = _CountingScheduleRepository()
    character = _character()
    terrace = _activity(
        13, 14, "在咖啡廳露天座位看書", location="露天座位",
        scene_privacy=ScenePrivacy.PUBLIC,
        meeting_affordance=MeetingAffordance.OPEN_TO_ENCOUNTER,
    )
    await _seed(repo, character, [terrace])
    judge = _StubJudge(
        (
            ActivityWeatherRewrite(
                activity_id=terrace.id,
                description="雨下起來了，換到咖啡廳室內的角落看書",
                location="咖啡廳室內",
            ),
        ),
    )
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    vetted = await service.vet(character, now=NOW)

    assert vetted is not None
    assert vetted.activities[0].scene_privacy is None
    assert vetted.activities[0].meeting_affordance is None


@pytest.mark.asyncio
async def test_untouched_setting_keeps_the_planner_scene_verdict() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    walk = _activity(
        13, 14, "河堤散步",
        scene_privacy=ScenePrivacy.PUBLIC,
        meeting_affordance=MeetingAffordance.OPEN_TO_ENCOUNTER,
    )
    other = _activity(14, 15, "咖啡廳讀書")
    await _seed(repo, character, [walk, other])
    judge = _StubJudge(
        (
            # Only the busy score moved — the setting is unchanged, so the
            # planner's scene verdict still describes the truth.
            ActivityWeatherRewrite(activity_id=walk.id, busy_score=0.5),
        ),
    )
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    vetted = await service.vet(character, now=NOW)

    assert vetted is not None
    assert vetted.activities[0].scene_privacy is ScenePrivacy.PUBLIC
    assert vetted.activities[0].meeting_affordance is MeetingAffordance.OPEN_TO_ENCOUNTER


# ---------------------------------------------------------------------- #
# Concurrent writers during the (multi-second) judge call
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_writes_landing_during_the_judge_call_survive() -> None:
    # The judge takes seconds; the memorialiser, the encounter runner and the
    # chat commitment extractor all write to the same day meanwhile. The vet
    # must merge its verdict onto the day as it stands NOW, never persist the
    # snapshot it read before the call.
    repo = _CountingScheduleRepository()
    character = _character()
    walk = _activity(13, 14, "河堤散步")
    morning = _activity(8, 9, "晨跑")
    await _seed(repo, character, [morning, walk])
    evening = _activity(18, 19, "和你約好的晚餐")

    async def concurrent_writer() -> None:
        stored = await repo.get(character.id, DAY)
        await repo.claim_memorialize([morning.id])
        stored = await repo.get(character.id, DAY)
        await repo.save(stored.with_activities([*stored.activities, evening]))

    judge = _StubJudge(
        (
            ActivityWeatherRewrite(
                activity_id=walk.id, description="雨太大，改在家裡窗邊看雨",
            ),
        ),
        during=concurrent_writer,
    )
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=NOW)

    stored = await repo.get(character.id, DAY)
    by_id = {activity.id: activity for activity in stored.activities}
    # The concurrent memorialize claim is intact (reverting it would re-open an
    # exactly-once claim and duplicate the episodic memory)…
    assert by_id[morning.id].memorialized is True
    # …the concurrently-added block is still there…
    assert evening.id in by_id
    # …and the verdict still landed.
    assert by_id[walk.id].description == "雨太大，改在家裡窗邊看雨"


@pytest.mark.asyncio
async def test_marker_only_pass_does_not_revert_a_concurrent_write() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    morning = _activity(8, 9, "晨跑")
    await _seed(repo, character, [morning, _activity(13, 14, "河堤散步")])

    async def concurrent_writer() -> None:
        await repo.claim_memorialize([morning.id])

    judge = _StubJudge(during=concurrent_writer)
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=NOW)

    stored = await repo.get(character.id, DAY)
    assert stored.activities[0].memorialized is True
    assert stored.weather_vet_condition == "大雨"


@pytest.mark.asyncio
async def test_rewrite_of_a_block_that_vanished_mid_call_is_dropped() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    walk = _activity(13, 14, "河堤散步")
    reading = _activity(14, 15, "咖啡廳讀書")
    await _seed(repo, character, [walk, reading])

    async def concurrent_writer() -> None:
        stored = await repo.get(character.id, DAY)
        await repo.save(
            stored.with_activities(
                [a for a in stored.activities if a.id != walk.id],
            ),
        )

    judge = _StubJudge(
        (
            ActivityWeatherRewrite(activity_id=walk.id, description="改在家裡"),
            ActivityWeatherRewrite(activity_id=reading.id, description="改成室內閱讀"),
        ),
        during=concurrent_writer,
    )
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    vetted = await service.vet(character, now=NOW)

    assert vetted is not None
    # The deleted block does not come back from the dead; the survivor is patched.
    assert [a.id for a in vetted.activities] == [reading.id]
    assert vetted.activities[0].description == "改成室內閱讀"


@pytest.mark.asyncio
async def test_rewrite_of_a_block_memorialized_mid_call_is_dropped() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    walk = _activity(13, 14, "河堤散步")
    await _seed(repo, character, [walk])

    async def concurrent_writer() -> None:
        await repo.claim_memorialize([walk.id])

    judge = _StubJudge(
        (ActivityWeatherRewrite(activity_id=walk.id, description="改在家裡"),),
        during=concurrent_writer,
    )
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    await service.vet(character, now=NOW)

    stored = await repo.get(character.id, DAY)
    # Memorialised means the memory of it is already written — rewriting the
    # prose afterwards would leave the day and the memory disagreeing.
    assert stored.activities[0].description == "河堤散步"
    assert stored.activities[0].memorialized is True


@pytest.mark.asyncio
async def test_day_deleted_mid_call_is_not_resurrected() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    walk = _activity(13, 14, "河堤散步")
    await _seed(repo, character, [walk])

    async def concurrent_writer() -> None:
        await repo.delete_for_character(character.id)

    judge = _StubJudge(
        (ActivityWeatherRewrite(activity_id=walk.id, description="改在家裡"),),
        during=concurrent_writer,
    )
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    assert await service.vet(character, now=NOW) is None
    assert await repo.get(character.id, DAY) is None


@pytest.mark.asyncio
async def test_operator_language_and_location_reach_the_ports() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])
    judge = _StubJudge()
    weather = _StubWeather(RAIN_BLOCK)
    service = _service(
        repository=repo, judge=judge, weather=weather,
        operator=_operator(language="ja-JP"),
    )

    await service.vet(character, now=NOW)

    assert judge.languages == ["ja-JP"]
    assert weather.locations[0] is not None
    assert weather.locations[0].label == "台北"


# ---------------------------------------------------------------------- #
# Fail-soft
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_judge_crash_leaves_the_day_untouched_and_the_gate_open() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])
    judge = _StubJudge(crash=True)
    service = _service(repository=repo, judge=judge, weather=_StubWeather(RAIN_BLOCK))

    saves_before = repo.saves
    assert await service.vet(character, now=NOW) is None
    # Nothing persisted — a judge that never spoke has not vetted anything,
    # so the next tick retries instead of skipping on a stamped marker.
    assert repo.saves == saves_before
    stored = await repo.get(character.id, DAY)
    assert stored.weather_vet_activity_id is None
    assert stored.activities[0].description == "河堤散步"


@pytest.mark.asyncio
async def test_weather_port_crash_is_contained() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])

    class _BrokenWeather:
        async def describe(self, *, now=None, location=None) -> str:  # noqa: ANN001
            raise RuntimeError("upstream down")

    judge = _StubJudge()
    service = ScheduleWeatherDriftService(
        schedule_repository=repo,
        drift_port=judge,
        local_tz=UTC,
        weather_context_port=_BrokenWeather(),
        operator_profile_service=_StubOperatorProfileService(_operator()),
    )

    assert await service.vet(character, now=NOW) is None
    assert judge.windows == []


@pytest.mark.asyncio
async def test_repository_crash_is_contained() -> None:
    character = _character()

    class _BrokenRepository(InMemoryScheduleRepository):
        async def get(self, character_id, date_):  # noqa: ANN001
            raise RuntimeError("db down")

    judge = _StubJudge()
    service = _service(
        repository=_BrokenRepository(), judge=judge,
        weather=_StubWeather(RAIN_BLOCK),
    )

    assert await service.vet(character, now=NOW) is None
    assert judge.windows == []


@pytest.mark.asyncio
async def test_operator_lookup_failure_falls_back_to_site_timezone() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])

    class _BrokenOperatorProfileService:
        async def get_for_user(self, user_id: str):  # noqa: ANN001
            raise RuntimeError("profile store down")

    judge = _StubJudge()
    service = ScheduleWeatherDriftService(
        schedule_repository=repo,
        drift_port=judge,
        local_tz=UTC,
        weather_context_port=_StubWeather(RAIN_BLOCK),
        operator_profile_service=_BrokenOperatorProfileService(),
    )

    vetted = await service.vet(character, now=NOW)

    # The day still gets vetted — the site timezone and the ship-first
    # language carry it through.
    assert vetted is not None
    assert judge.languages == ["zh-TW"]


@pytest.mark.asyncio
async def test_unwired_judge_is_a_no_op() -> None:
    repo = _CountingScheduleRepository()
    character = _character()
    await _seed(repo, character, [_activity(13, 14, "河堤散步")])
    service = ScheduleWeatherDriftService(
        schedule_repository=repo,
        drift_port=None,
        local_tz=UTC,
        weather_context_port=_StubWeather(RAIN_BLOCK),
        operator_profile_service=_StubOperatorProfileService(_operator()),
    )

    saves_before = repo.saves
    assert await service.vet(character, now=NOW) is None
    assert repo.saves == saves_before
