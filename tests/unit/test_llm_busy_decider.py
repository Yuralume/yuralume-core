"""Parser-level tests for :class:`LLMBusyReplyDecider`.

Mirrors the shape of ``test_llm_idle_drift`` — exercises response
extraction and sanitisation without hitting a real model.

We test the *contract* the adapter promises (per project's top
directive — never test LLM content choices):

* Well-formed defer response yields the right ``BusyDecision``.
* "immediate" / blank / unparseable → fail-soft empty defer.
* Brief reply over the char cap is dropped (model wrote a full reply).
* HH:MM defer-until uses the character local tz.
* Past HH:MM rolls forward one day (model meant "later today" but it
  rolled over).
* Defer-until below the minimum lead is clamped up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.contracts.busy_reply_decider import BusyReplyMode
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.personality_type import CharacterPersonalityType
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.busy.llm_decider import (
    LLMBusyReplyDecider,
    _MIN_DEFER_LEAD,
    _parse,
)


class _StubModel(ChatModelPort):
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.prompts: list[str] = []

    @property
    def supports_vision(self) -> bool:  # pragma: no cover - unused
        return False

    async def generate(self, prompt: str, **kwargs: object) -> str:
        self.prompts.append(prompt)
        return self.response

    async def generate_stream(  # pragma: no cover - unused
        self, prompt: str, **kwargs: object,
    ) -> AsyncIterator[str]:
        yield self.response


def _character(**overrides) -> Character:  # noqa: ANN003
    base = dict(
        name="Airi",
        summary="社畜 OL",
        personality=["責任感重"],
        interests=[],
        speaking_style="平淡",
        boundaries=[],
        state=CharacterState(
            emotion="專注", affection=60, fatigue=30, trust=50, energy=70,
        ),
    )
    base.update(overrides)
    return Character.create(**base)


def _activity(*, busy: float = 0.85) -> ScheduleActivity:
    start = datetime(2026, 5, 16, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 16, 15, 0, tzinfo=timezone.utc)
    return ScheduleActivity.create(
        start_at=start,
        end_at=end,
        description="跟客戶開會",
        category="meeting",
        location="conference room",
        busy_score=busy,
    )


def _now() -> datetime:
    return datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)


class TestParse:
    def test_immediate_mode_returns_empty_defer(self) -> None:
        raw = "模式：立即\n短回覆：\n延後到：\n原因：\n"
        decision = _parse(raw, now=_now(), current_activity=_activity(), local_tz=timezone.utc)
        assert decision.mode == BusyReplyMode.IMMEDIATE
        assert decision.is_defer is False

    def test_defer_extracts_fields(self) -> None:
        raw = (
            "模式：延後\n"
            "短回覆：先回，會議結束我再好好回你\n"
            "延後到：15:30\n"
            "原因：會議中"
        )
        decision = _parse(
            raw, now=_now(), current_activity=_activity(), local_tz=timezone.utc,
        )
        assert decision.is_defer is True
        assert decision.brief_reply == "先回，會議結束我再好好回你"
        assert decision.defer_reason == "會議中"
        assert decision.defer_until is not None
        assert decision.defer_until > _now()

    def test_blank_input_is_immediate(self) -> None:
        assert _parse(
            "", now=_now(), current_activity=_activity(), local_tz=timezone.utc,
        ).mode == BusyReplyMode.IMMEDIATE

    def test_defer_without_brief_falls_back_to_immediate(self) -> None:
        raw = "模式：延後\n短回覆：\n延後到：15:30\n原因：忙"
        decision = _parse(raw, now=_now(), current_activity=_activity(), local_tz=timezone.utc)
        assert decision.is_defer is False

    def test_brief_over_cap_dropped(self) -> None:
        # The brief reply ceiling drops outputs that wandered into full
        # reply territory — model misread the schema.
        long_reply = "啊" * 200
        raw = (
            f"模式：延後\n短回覆：{long_reply}\n延後到：15:30\n原因：忙"
        )
        decision = _parse(raw, now=_now(), current_activity=_activity(), local_tz=timezone.utc)
        assert decision.is_defer is False

    def test_defer_until_blank_falls_back_to_activity_end(self) -> None:
        raw = "模式：延後\n短回覆：等一下\n延後到：\n原因：忙"
        activity = _activity()
        decision = _parse(
            raw, now=_now(), current_activity=activity, local_tz=timezone.utc,
        )
        assert decision.is_defer is True
        assert decision.defer_until == activity.end_at

    def test_hhmm_uses_owner_timezone(self) -> None:
        raw = "模式：延後\n短回覆：等一下\n延後到：18:30\n原因：忙"
        decision = _parse(
            raw,
            now=datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc),
            current_activity=_activity(),
            local_tz=ZoneInfo("Asia/Taipei"),
        )
        assert decision.is_defer is True
        assert decision.defer_until == datetime(
            2026, 6, 15, 10, 30, tzinfo=timezone.utc,
        )

    def test_past_hhmm_rolls_forward_one_day(self) -> None:
        raw = "模式：延後\n短回覆：等一下\n延後到：12:00\n原因：忙"
        decision = _parse(
            raw, now=_now(), current_activity=_activity(), local_tz=timezone.utc,
        )
        assert decision.is_defer is True
        assert decision.defer_until is not None
        # _now is 14:30 UTC → 12:00 today UTC has passed → bumped 24h
        delta = decision.defer_until - _now()
        assert delta.total_seconds() > 0
        assert delta.total_seconds() >= 21 * 3600  # ≥ ~21h ahead

    def test_min_defer_lead_clamps(self) -> None:
        # If model writes a defer_until in the immediate past, the
        # dispatcher would release on the very next tick before the
        # client even sees the ack render — clamp to ≥ _MIN_DEFER_LEAD.
        raw = "模式：延後\n短回覆：等下\n延後到：14:29\n原因：忙"
        decision = _parse(
            raw, now=_now(), current_activity=_activity(), local_tz=timezone.utc,
        )
        assert decision.is_defer is True
        assert decision.defer_until is not None
        # past HHMM is bumped forward 1 day, well above min_lead
        assert decision.defer_until - _now() >= _MIN_DEFER_LEAD

    def test_strips_code_fence(self) -> None:
        raw = "```\n模式：延後\n短回覆：等等\n延後到：15:30\n原因：忙\n```"
        decision = _parse(raw, now=_now(), current_activity=_activity(), local_tz=timezone.utc)
        assert decision.is_defer is True


class TestDecide:
    @pytest.mark.asyncio
    async def test_empty_user_message_short_circuits(self) -> None:
        model = _StubModel(response="模式：延後\n短回覆：等等\n")
        decider = LLMBusyReplyDecider(model=model)
        decision = await decider.decide(
            character=_character(),
            user_message="   ",
            current_activity=_activity(),
            now=_now(),
        )
        assert decision.is_defer is False
        assert model.prompts == []

    @pytest.mark.asyncio
    async def test_llm_crash_returns_immediate(self) -> None:
        class _Boom(ChatModelPort):
            supports_vision = False

            async def generate(self, prompt: str, **kwargs: object) -> str:
                raise RuntimeError("backend down")

            async def generate_stream(  # pragma: no cover - unused
                self, prompt: str, **kwargs: object,
            ) -> AsyncIterator[str]:
                yield ""

        decider = LLMBusyReplyDecider(model=_Boom())
        decision = await decider.decide(
            character=_character(),
            user_message="你還在嗎",
            current_activity=_activity(),
            now=_now(),
        )
        assert decision.is_defer is False


def _sent_outreach(*, message: str, minutes_ago: int) -> ProactiveAttempt:
    return ProactiveAttempt.record(
        character_id="c1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        message=message,
        now=_now() - timedelta(minutes=minutes_ago),
    )


class TestProactiveOutreachContext:
    """A reply to the character's own just-sent proactive push must reach
    the decider as context, so it doesn't brush the user off with the
    busy mechanism right after reaching out."""

    @pytest.mark.asyncio
    async def test_recent_outreach_rendered_into_prompt(self) -> None:
        model = _StubModel(response="模式：立即\n短回覆：\n延後到：\n原因：\n")
        decider = LLMBusyReplyDecider(model=model)
        await decider.decide(
            character=_character(),
            user_message="我也想你，會議加油",
            current_activity=_activity(),
            recent_proactive_attempts=(
                _sent_outreach(message="在開會但突然好想你", minutes_ago=4),
            ),
            now=_now(),
        )
        assert model.prompts
        prompt = model.prompts[0]
        # The rendered fact line — distinct from the static judgement
        # principle in the template, which only names the block.
        assert "約 4 分鐘前，你主動傳了：「在開會但突然好想你」" in prompt

    @pytest.mark.asyncio
    async def test_no_outreach_renders_no_block(self) -> None:
        model = _StubModel(response="模式：立即\n")
        decider = LLMBusyReplyDecider(model=model)
        await decider.decide(
            character=_character(),
            user_message="你在嗎",
            current_activity=_activity(),
            now=_now(),
        )
        assert model.prompts
        # The static template still names the block in its judgement
        # principle; only the rendered fact line ("你主動傳了") must be
        # absent when there is no recent outreach.
        assert "你主動傳了" not in model.prompts[0]


class TestRelationshipAndPersonalityContext:
    @pytest.mark.asyncio
    async def test_prompt_includes_relationship_personality_and_qualitative_state(
        self,
    ) -> None:
        model = _StubModel(response="模式：立即\n短回覆：\n延後到：\n原因：\n")
        decider = LLMBusyReplyDecider(model=model)
        await decider.decide(
            character=_character(
                personality_type=CharacterPersonalityType(
                    code="ISFJ",
                    source="user_explicit",
                    confidence=0.9,
                    rationale="重視照顧與穩定關係。",
                    consistency_notes=("人設優先於類型。",),
                ),
            ),
            user_message="你在忙嗎",
            current_activity=_activity(),
            relationship_context_lines=(
                "使用者創角時確認的起始關係設定：",
                "- 關係：剛認識的同事",
            ),
            interaction_context_lines=(
                "- 與使用者互動熱度：互動還很少；不要把忙碌變成冷落。",
            ),
            now=_now(),
        )

        prompt = model.prompts[0]
        assert "16 型性格參考" in prompt
        assert "ISFJ" in prompt
        assert "重視照顧與穩定關係" in prompt
        assert "起始關係設定" in prompt
        assert "剛認識的同事" in prompt
        assert "互動熱度" in prompt
        assert "互動還很少" in prompt
        assert "好感狀態：關係友好" in prompt
        assert "信任狀態：中性" in prompt
        assert "好感 60" not in prompt
        assert "精力 70" not in prompt
        assert "疲勞 30" not in prompt


class TestSleepGuidance:
    """Sleep is deliberately *not* a hard-defer situation: a message wakes
    the character, who usually sends a groggy immediate reply instead of
    silently deferring until morning (owner trade-off, 2026-06-12)."""

    @pytest.mark.asyncio
    async def test_prompt_treats_sleep_as_being_woken(self) -> None:
        model = _StubModel(response="模式：立即\n短回覆：\n延後到：\n原因：\n")
        decider = LLMBusyReplyDecider(model=model)
        await decider.decide(
            character=_character(),
            user_message="睡了嗎",
            current_activity=_activity(),
            now=_now(),
        )
        prompt = model.prompts[0]
        assert "吵醒" in prompt
        # Sleep must no longer be listed among the extreme can't-reply
        # examples — those stay reserved for exam/driving/on-stage cases.
        assert "睡著、考試" not in prompt
