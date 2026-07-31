"""CF5 — planner sees the days just gone, and is told not to repeat them.

The defect: ``plan_day`` had no input describing adjacent days, and
``schedule/planner.txt`` had no cross-day anti-repetition rule. One
one-off activity was scheduled onto three separate days, and the same
handful of motifs recycled indefinitely.

These tests assert the two halves of the fix at the prompt boundary:

1. the recent-day fact block renders (dates + descriptions verbatim), and
   stays absent when there is no history;
2. the freshness rules — including the routine exemption — are standing
   template principles, present whether or not today happens to have
   history attached.

Whether the model *obeys* the rule is not unit-testable; the input being
correct and the rule being present is.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.domain.entities.behavioral_pattern import (
    KIND_RECURRING_ACTIVITY,
    BehavioralPattern,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    OPERATOR_INVITE_EXPIRED_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.services.recent_activity_digest import (
    RecentDayActivities,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.infrastructure.schedule.llm_planner import LLMSchedulePlanner

UTC = timezone.utc

TARGET = date(2026, 5, 22)

_RECENT_BLOCK_HEADER = "最近幾天已經排過／做過的活動"
# The header alone is not a presence test: the standing rule refers to the
# block *by name*, so it appears in the template whether or not there is any
# history. This parenthetical only ever ships with the fact list itself.
_RECENT_BLOCK_MARKER = "事實清單；用來判斷什麼題材剛用過"


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


def _recent(day: date, *descriptions: str) -> RecentDayActivities:
    return RecentDayActivities(date=day, descriptions=tuple(descriptions))


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
async def test_recent_activity_block_carries_dates_and_descriptions() -> None:
    prompt = await _prompt_for(
        recent_activity_digest=(
            _recent(date(2026, 5, 20), "重看《某作品》第 4 話", "去巷口的刨冰店"),
            _recent(date(2026, 5, 21), "在共享工作室畫分鏡"),
        ),
    )

    assert _RECENT_BLOCK_HEADER in prompt
    assert _RECENT_BLOCK_MARKER in prompt
    # Absolute dates, never "前天/昨天" — the same discipline CF3 applied to
    # expired commitments.
    assert "2026-05-20" in prompt
    assert "2026-05-21" in prompt
    assert "重看《某作品》第 4 話" in prompt
    assert "去巷口的刨冰店" in prompt
    assert "在共享工作室畫分鏡" in prompt


@pytest.mark.asyncio
async def test_recent_activity_block_absent_without_history() -> None:
    """A brand-new character (or a wiped repo) renders no fact list at all
    — the standing rule stays, but there is nothing for it to point at."""
    prompt = await _prompt_for()

    assert _RECENT_BLOCK_MARKER not in prompt
    assert "2026-05-2" not in prompt.replace(TARGET.isoformat(), "")


@pytest.mark.asyncio
async def test_recent_activity_block_orders_days_chronologically() -> None:
    prompt = await _prompt_for(
        recent_activity_digest=(
            _recent(date(2026, 5, 19), "A 活動"),
            _recent(date(2026, 5, 20), "B 活動"),
            _recent(date(2026, 5, 21), "C 活動"),
        ),
    )

    assert prompt.index("2026-05-19") < prompt.index("2026-05-20")
    assert prompt.index("2026-05-20") < prompt.index("2026-05-21")


# --------------------------------------------------------------------- #
# The standing rules.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_freshness_rules_are_standing_template_principles() -> None:
    """Present with no history attached: the dialogue summary and the arc
    can echo a stale motif regardless of what the schedule rows hold."""
    prompt = await _prompt_for(recent_dialogue_summary="兩人又聊到那部作品")

    assert "生活素材新鮮度紀律（一）" in prompt
    assert "生活素材新鮮度紀律（二）" in prompt
    assert "生活素材新鮮度紀律（三）" in prompt
    # (一) bans the repeat, (二) demands progress when a theme returns.
    assert "不得與清單裡已經出現過的重複" in prompt
    assert "必須帶明確的新進展或變化" in prompt
    # Reworded / relocated reruns are covered too, same as CF3's expiry rule.
    assert "換個說法、換個時段、換個地點寫同一件事一樣算重複" in prompt


@pytest.mark.asyncio
async def test_routine_activities_are_exempt_from_the_anti_repetition_rule() -> None:
    """The red line on this ticket: sleep / meals / commute / observed
    habits must NOT be swept up by the freshness rule, or the day loses
    its rhythm entirely."""
    prompt = await _prompt_for(
        recurring_patterns=(
            BehavioralPattern.new(
                character_id="char-A",
                kind=KIND_RECURRING_ACTIVITY,
                description="星期一早晨常去晨跑",
                observed_count=4,
                salience=0.7,
                last_observed_at=datetime(2026, 5, 21, tzinfo=UTC),
            ),
        ),
    )

    assert "只約束特色／一次性活動" in prompt
    assert "睡眠、三餐" in prompt
    assert "通勤" in prompt
    # The exemption points at the observed-rhythm block so the two inputs
    # cannot be read as contradicting each other. Referenced the way the
    # existing 約定時效紀律 rule references it — loosely, not by the exact
    # rendered header, which only exists when there are patterns to render.
    assert "「生活節奏」區塊列出的慣例活動" in prompt
    assert "近幾週觀察到的生活節奏" in prompt, "本測試有供 patterns，事實區塊應同時在場"
    assert "不要為了避開重複" in prompt


@pytest.mark.asyncio
async def test_block_repeats_the_rule_next_to_the_facts() -> None:
    """The rule that bites lives at the top; the fact list restates it so
    the model doesn't have to hold 40 lines of principles in working
    memory to know what the list is for."""
    prompt = await _prompt_for(
        recent_activity_digest=(_recent(date(2026, 5, 21), "去某間特定店家"),),
    )

    tail = prompt[prompt.index(_RECENT_BLOCK_MARKER):]
    assert "不要再排一次" in tail
    assert "日常慣例" in tail


# --------------------------------------------------------------------- #
# Non-regression on the disciplines CF3 / earlier tickets landed.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_existing_disciplines_survive_the_freshness_rules() -> None:
    expired = ScheduleActivity.create(
        start_at=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
        end_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
        description="沒去成的那攤",
        category="social",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id="user-1",
                display_name="你",
                role=OPERATOR_INVITE_EXPIRED_ROLE,
            ),
        ),
    )
    commitment = ScheduleActivity.create(
        start_at=datetime(2026, 5, 22, 19, 0, tzinfo=UTC),
        end_at=datetime(2026, 5, 22, 21, 0, tzinfo=UTC),
        description="跟使用者看電影",
        category="leisure",
    )

    prompt = await _prompt_for(
        pre_committed_activities=(commitment,),
        expired_operator_commitments=(expired,),
        recent_activity_digest=(_recent(date(2026, 5, 21), "在家整理房間"),),
    )

    assert "天氣紀律（一）" in prompt
    assert "天氣紀律（二）" in prompt
    assert "約定時效紀律（一）" in prompt
    assert "約定時效紀律（二）" in prompt
    assert "必須原封不動保留在輸出中" in prompt
    assert "已經過期、沒有成行的邀請／共同約定" in prompt
    assert "沒去成的那攤" in prompt


def test_template_still_declares_every_context_slot() -> None:
    """Guard against a future edit silently unhooking a block: the slots
    are what make the Python-side fact rendering reach the model at all."""
    template = get_default_loader().raw("schedule/planner")

    for slot in (
        "${commitments_block}",
        "${expired_commitments_block}",
        "${recent_activity_block}",
        "${patterns_block}",
        "${dialogue_block}",
        "${weather_block}",
    ):
        assert slot in template, f"planner 模板缺少插槽 {slot}"
