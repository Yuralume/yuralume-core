"""SE1 — planner sees the character's recent story events, as inspiration.

The defect (stale-appointment diagnosis, "planner 盲區" item): ``plan_day``'s
only "what has been going on" input was the recent-dialogue summary — which
is mostly the character's own proactive output coming back — so the daily
gacha/arc story events never reached the planner and the same handful of
motifs recycled indefinitely.

These tests assert the prompt boundary:

1. the story-event fact block renders (absolute dates + narratives), stays
   absent when there are no events, and is bounded (count cap + narrative
   truncation);
2. the standing diversity rules（四）（五）are template principles, present
   regardless of whether today happens to have events attached;
3. with empty inputs the rendered prompt is byte-identical to the
   no-new-kwargs call, and a populated prompt differs from the empty one by
   exactly the new block — nothing else moved.

Whether the model *obeys* the rules is not unit-testable; the input being
correct and the rules being present is.
"""

from __future__ import annotations

from datetime import date, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.services.recent_activity_digest import (
    RecentDayActivities,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.infrastructure.schedule.llm_planner import (
    _MAX_STORY_EVENT_NARRATIVE_CHARS,
    _MAX_STORY_EVENTS,
    _render_story_events_block,
    LLMSchedulePlanner,
)

UTC = timezone.utc

TARGET = date(2026, 5, 22)

# The standing rule（五）refers to the block *by name*, so the header text
# appears in the template whether or not there are events. Only this
# parenthetical ships with the fact list itself.
_BLOCK_HEADER = "角色近日發生的小事"
_BLOCK_MARKER = "由舊到新；靈感素材，不是指令"

_RECENT_ACTIVITY_MARKER = "事實清單；用來判斷什麼題材剛用過"


class _CapturingModel:
    def __init__(self) -> None:
        self.last_prompt = ""

    async def generate(self, prompt: str) -> str:
        self.last_prompt = prompt
        return "[]"

    def generate_stream(self, prompt: str):  # pragma: no cover
        async def _empty():
            if False:
                yield ""
        return _empty()


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _event(
    day: date,
    narrative: str,
    *,
    seed_id: str = "seed-1",
    tone: str | None = None,
) -> StoryEvent:
    return StoryEvent.create(
        character_id="char-1",
        date=day.isoformat(),
        seed_id=seed_id,
        narrative=narrative,
        emotional_tone=tone,
    )


async def _prompt_for(**kwargs: object) -> str:
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)
    await planner.plan_day(
        character=_character(), date_=TARGET, local_tz=UTC, **kwargs,
    )
    return model.last_prompt


# --------------------------------------------------------------------- #
# The fact block.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_story_events_block_carries_dates_and_narratives() -> None:
    prompt = await _prompt_for(
        recent_story_events=(
            _event(
                date(2026, 5, 19),
                "在舊書店翻到一本絕版詩集，站著讀完了半本。",
                seed_id="seed-a",
            ),
            _event(
                TARGET,
                "早上收到學妹的訊息，說想請我幫忙看她的分鏡稿。",
                seed_id="seed-b",
            ),
        ),
    )

    assert _BLOCK_MARKER in prompt
    # Absolute dates, never 前天/昨天 — the CF3/CF5 discipline.
    assert "2026-05-19" in prompt
    assert "在舊書店翻到一本絕版詩集" in prompt
    assert "說想請我幫忙看她的分鏡稿" in prompt


@pytest.mark.asyncio
async def test_story_events_block_absent_without_events() -> None:
    prompt = await _prompt_for()

    assert _BLOCK_MARKER not in prompt


@pytest.mark.asyncio
async def test_story_events_render_oldest_first() -> None:
    prompt = await _prompt_for(
        recent_story_events=(
            _event(date(2026, 5, 18), "第一件小事", seed_id="s1"),
            _event(date(2026, 5, 20), "第二件小事", seed_id="s2"),
            _event(date(2026, 5, 21), "第三件小事", seed_id="s3"),
        ),
    )

    assert prompt.index("第一件小事") < prompt.index("第二件小事")
    assert prompt.index("第二件小事") < prompt.index("第三件小事")


@pytest.mark.asyncio
async def test_narrative_is_truncated() -> None:
    long_narrative = "很長的敘事" * 80  # 400 chars, well past the cap
    prompt = await _prompt_for(
        recent_story_events=(_event(date(2026, 5, 20), long_narrative),),
    )

    assert long_narrative not in prompt
    truncated = long_narrative[:_MAX_STORY_EVENT_NARRATIVE_CHARS] + "…"
    assert truncated in prompt


@pytest.mark.asyncio
async def test_event_count_is_capped_keeping_the_most_recent() -> None:
    events = tuple(
        _event(
            date(2026, 5, 10 + offset),
            f"第 {offset} 天的小事",
            seed_id=f"seed-{offset}",
        )
        for offset in range(_MAX_STORY_EVENTS + 2)
    )
    prompt = await _prompt_for(recent_story_events=events)

    # Oldest two fall off; the most recent _MAX_STORY_EVENTS stay.
    assert "第 0 天的小事" not in prompt
    assert "第 1 天的小事" not in prompt
    for offset in range(2, _MAX_STORY_EVENTS + 2):
        assert f"第 {offset} 天的小事" in prompt


@pytest.mark.asyncio
async def test_emotional_tone_is_surfaced_when_present() -> None:
    prompt = await _prompt_for(
        recent_story_events=(
            _event(date(2026, 5, 20), "路上被突來的陣雨淋濕", tone="懊惱"),
        ),
    )

    assert "懊惱" in prompt


# --------------------------------------------------------------------- #
# None-safe / byte-compatibility locks.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_omitting_the_kwarg_matches_passing_empty() -> None:
    """The two ways a caller can supply "no events" render identical bytes
    — an unwired repository cannot change planner behaviour."""
    assert await _prompt_for() == await _prompt_for(recent_story_events=())


@pytest.mark.asyncio
async def test_populated_prompt_differs_by_exactly_the_new_block() -> None:
    """Byte-level lock on "僅多了新區塊": excising the rendered block from
    the populated prompt reproduces the empty-input prompt exactly."""
    events = (
        _event(date(2026, 5, 20), "去河堤看了黃昏", seed_id="seed-a"),
        _event(TARGET, "收到一封舊友的明信片", seed_id="seed-b"),
    )
    prompt_with = await _prompt_for(recent_story_events=events)
    prompt_without = await _prompt_for()

    block = _render_story_events_block(events)
    assert block
    assert block in prompt_with
    assert prompt_with.replace(block, "") == prompt_without


# --------------------------------------------------------------------- #
# Four quadrants: story events × recent-day activities.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("with_events", [True, False])
@pytest.mark.parametrize("with_digest", [True, False])
async def test_story_events_and_activity_digest_are_independent(
    with_events: bool, with_digest: bool,
) -> None:
    kwargs: dict[str, object] = {}
    if with_events:
        kwargs["recent_story_events"] = (
            _event(date(2026, 5, 21), "巷口的貓終於肯讓我摸了"),
        )
    if with_digest:
        kwargs["recent_activity_digest"] = (
            RecentDayActivities(
                date=date(2026, 5, 21),
                descriptions=("去巷口的刨冰店",),
            ),
        )

    prompt = await _prompt_for(**kwargs)

    assert (_BLOCK_MARKER in prompt) is with_events
    assert ("巷口的貓終於肯讓我摸了" in prompt) is with_events
    assert (_RECENT_ACTIVITY_MARKER in prompt) is with_digest
    assert ("去巷口的刨冰店" in prompt) is with_digest


# --------------------------------------------------------------------- #
# The standing rules.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_diversity_rules_are_standing_template_principles() -> None:
    """Present with no events / history attached — the dialogue summary
    and the arc can echo a stale motif regardless of what today carries."""
    prompt = await _prompt_for()

    assert "生活素材新鮮度紀律（四）" in prompt
    assert "生活素材新鮮度紀律（五）" in prompt
    # （四）type-level continuity: avoid re-scheduling a run of the same
    # kind of activity unless there is an explicit reason.
    assert "避免再排同類型" in prompt
    assert "除非有明確理由" in prompt
    assert "新的一天優先讓生活有變化" in prompt
    # （五）story events are inspiration, never instructions.
    assert _BLOCK_HEADER in prompt
    assert "素材不是指令" in prompt


@pytest.mark.asyncio
async def test_block_repeats_the_rule_next_to_the_facts() -> None:
    prompt = await _prompt_for(
        recent_story_events=(_event(date(2026, 5, 21), "撿到一把壞掉的傘"),),
    )

    tail = prompt[prompt.index(_BLOCK_MARKER):]
    assert "也可以完全不提" in tail
    assert "不要把同一件事原樣重演" in tail


def test_template_declares_the_story_events_slot() -> None:
    template = get_default_loader().raw("schedule/planner")

    assert "${story_events_block}" in template
