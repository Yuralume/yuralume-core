"""KB2 — beat lineage on the block the planner reserves for the scene.

The planner model marks *which* block is the day's beat (a boolean);
code stamps ``ScheduleActivity.source_beat_id`` from the day's beat. That
lineage is what lets the schedule memorializer keep a planned scene out
of episodic memory, so these tests pin both halves: the mark reaches the
prompt as a boolean, and the id only ever comes from the beat itself.
"""

from __future__ import annotations

from datetime import date, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import StoryArcBeat
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.schedule.llm_planner import LLMSchedulePlanner

UTC = timezone.utc


class _FakeModel:
    def __init__(self, response: str) -> None:
        self._response = response

    async def generate(self, prompt: str) -> str:  # noqa: ARG002
        return self._response

    def generate_stream(self, prompt: str):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


class _CapturingModel:
    def __init__(self) -> None:
        self.prompt = ""

    async def generate(self, prompt: str) -> str:
        self.prompt = prompt
        return "[]"

    def generate_stream(self, prompt: str):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="插畫家",
        personality=["內向"],
        interests=["咖啡"],
        speaking_style="溫柔",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _beat(*, day: date, position: str | None = "central") -> StoryArcBeat:
    return StoryArcBeat.create(
        arc_id="arc-kb2",
        sequence=1,
        scheduled_date=day,
        title="銀環裂開以前",
        summary="她在林道上等著對方趕來",
        tension="climax",
        scene_type="conflict",
        location="後山林道",
        operator_position=position,
    )


def _payload(*, marked_index: int | None) -> str:
    """Two blocks — preparation and the scene — optionally marking one."""
    blocks = [
        '{"start":"09:00","end":"11:00","description":"整理裝備、確認路線",'
        '"category":"prep"}',
        '{"start":"14:00","end":"16:00","description":"後山林道那場戲的時段",'
        '"category":"story"}',
    ]
    if marked_index is not None:
        blocks[marked_index] = blocks[marked_index][:-1] + ',"beat_ref":true}'
    return "[" + ",".join(blocks) + "]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "position",
    ["central", "absent"],
    ids=["awaiting-player", "solo-staged"],
)
async def test_prompt_asks_for_a_boolean_mark_not_an_identifier(
    position: str,
) -> None:
    """Both today-beat framings reserve a slot, so both must ask for the
    mark — and neither may invite the model to write an id, which is how
    a hallucinated identifier would reach the column the memorializer
    trusts. Preparation stays unmarked: it is an hour really lived."""
    today = date(2026, 4, 18)
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)

    await planner.plan_day(
        character=_character(), date_=today, local_tz=UTC,
        today_beat=_beat(day=today, position=position), upcoming_beats=(),
    )

    assert '"beat_ref": true' in model.prompt
    assert "不要自己編任何編號或 id 填進去" in model.prompt
    assert "不要**標記" in model.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "position",
    ["central", "absent"],
    ids=["awaiting-player", "solo-staged"],
)
async def test_marked_block_is_stamped_with_todays_beat_id(
    position: str,
) -> None:
    today = date(2026, 4, 18)
    beat = _beat(day=today, position=position)
    planner = LLMSchedulePlanner(model=_FakeModel(_payload(marked_index=1)))

    schedule = await planner.plan_day(
        character=_character(), date_=today, local_tz=UTC,
        today_beat=beat, upcoming_beats=(),
    )

    assert [a.source_beat_id for a in schedule.activities] == [None, beat.id]


@pytest.mark.asyncio
async def test_unmarked_blocks_carry_no_lineage() -> None:
    """A day the planner marked nothing on is an ordinary day: every
    block stays memorialisable."""
    today = date(2026, 4, 18)
    planner = LLMSchedulePlanner(model=_FakeModel(_payload(marked_index=None)))

    schedule = await planner.plan_day(
        character=_character(), date_=today, local_tz=UTC,
        today_beat=_beat(day=today), upcoming_beats=(),
    )

    assert all(a.source_beat_id is None for a in schedule.activities)


@pytest.mark.asyncio
async def test_gap_day_beat_never_stamps_lineage() -> None:
    """On a gap day the schedule service promotes the *next* beat into
    the same argument so the planner can build anticipation. Nothing that
    day is that scene, and the preparation it plans is a real experience —
    a stamp here would erase a day the character actually lived."""
    today = date(2026, 4, 18)
    planner = LLMSchedulePlanner(model=_FakeModel(_payload(marked_index=1)))

    schedule = await planner.plan_day(
        character=_character(), date_=today, local_tz=UTC,
        today_beat=_beat(day=today + timedelta(days=2)), upcoming_beats=(),
    )

    assert all(a.source_beat_id is None for a in schedule.activities)


@pytest.mark.asyncio
async def test_model_supplied_identifier_is_never_written() -> None:
    """A model that answers with an id instead of a boolean has not made
    a mark — and has certainly not chosen the beat."""
    today = date(2026, 4, 18)
    payload = (
        '[{"start":"14:00","end":"16:00","description":"林道那場戲",'
        '"category":"story","beat_ref":"beat-9999"}]'
    )
    planner = LLMSchedulePlanner(model=_FakeModel(payload))

    schedule = await planner.plan_day(
        character=_character(), date_=today, local_tz=UTC,
        today_beat=_beat(day=today), upcoming_beats=(),
    )

    assert schedule.activities[0].source_beat_id is None


@pytest.mark.asyncio
async def test_only_the_first_marked_block_is_stamped() -> None:
    """A model that over-marks (every block gets ``beat_ref: true``) must
    not fan the lineage out across the whole day — that starves the
    memorializer, which skips every stamped block as "already the scene".
    Only the earliest-starting marked block keeps the id."""
    today = date(2026, 4, 18)
    beat = _beat(day=today)
    payload = (
        '[{"start":"09:00","end":"11:00","description":"整理裝備、確認路線",'
        '"category":"prep","beat_ref":true},'
        '{"start":"14:00","end":"16:00","description":"後山林道那場戲的時段",'
        '"category":"story","beat_ref":true}]'
    )
    planner = LLMSchedulePlanner(model=_FakeModel(payload))

    schedule = await planner.plan_day(
        character=_character(), date_=today, local_tz=UTC,
        today_beat=beat, upcoming_beats=(),
    )

    assert [a.source_beat_id for a in schedule.activities] == [beat.id, None]


@pytest.mark.asyncio
async def test_mark_without_an_arc_beat_stamps_nothing() -> None:
    """No beat scheduled for the day, no lineage — a stray mark cannot
    invent one."""
    planner = LLMSchedulePlanner(model=_FakeModel(_payload(marked_index=1)))

    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )

    assert all(a.source_beat_id is None for a in schedule.activities)
