"""LLMSchedulePlanner: pre_committed_activities prompt + merge."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    OPERATOR_CONFIRMED_LAPSED_ROLE,
    OPERATOR_INVITE_EXPIRED_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.schedule.llm_planner import (
    LLMSchedulePlanner,
    _merge_pre_commitments,
)


UTC = timezone.utc


class _CapturingModel:
    def __init__(self, response: str = "[]") -> None:
        self.response = response
        self.last_prompt = ""

    async def generate(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response

    def generate_stream(self, prompt: str):  # pragma: no cover
        async def _empty():
            if False:
                yield ""
        return _empty()


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _commitment(hour_start: int, hour_end: int, description: str) -> ScheduleActivity:
    target = date(2026, 5, 20)
    return ScheduleActivity.create(
        start_at=datetime(2026, 5, 20, hour_start, 0, tzinfo=UTC),
        end_at=datetime(2026, 5, 20, hour_end, 0, tzinfo=UTC),
        description=description,
        category="leisure",
    )


def _expired(
    description: str,
    *,
    role: str = OPERATOR_INVITE_EXPIRED_ROLE,
    day: date = date(2026, 5, 18),
) -> ScheduleActivity:
    start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)
    return ScheduleActivity.create(
        start_at=start,
        end_at=start + timedelta(hours=2),
        description=description,
        category="social",
        location="城南的老店",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id="user-1",
                display_name="你",
                role=role,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_commitment_block_lands_in_prompt() -> None:
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)
    commitment = _commitment(19, 21, "跟使用者看電影")
    await planner.plan_day(
        character=_character(),
        date_=date(2026, 5, 20),
        local_tz=UTC,
        pre_committed_activities=(commitment,),
    )
    assert "已既定的承諾時段" in model.last_prompt
    assert "跟使用者看電影" in model.last_prompt
    assert "19:00" in model.last_prompt


@pytest.mark.asyncio
async def test_planner_failure_preserves_commitments() -> None:
    """Even when LLM call crashes, commitments survive in the terminal plan."""

    class _BrokenModel:
        async def generate(self, prompt: str) -> str:
            raise RuntimeError("boom")

        def generate_stream(self, prompt: str):  # pragma: no cover
            async def _empty():
                if False:
                    yield ""
            return _empty()

    planner = LLMSchedulePlanner(model=_BrokenModel())
    commitment = _commitment(19, 21, "看電影")
    result = await planner.plan_day(
        character=_character(),
        date_=date(2026, 5, 20),
        local_tz=UTC,
        pre_committed_activities=(commitment,),
    )
    assert result.is_planned is True
    assert any(a.description == "看電影" for a in result.activities)


def test_merge_keeps_llm_block_when_it_covers_commitment_window() -> None:
    """When the LLM emitted an activity that already covers the
    commitment time, keep the LLM version (it may have richer
    description) — but don't end up with a duplicate."""
    llm_acts = [
        ScheduleActivity.create(
            start_at=datetime(2026, 5, 20, 19, 0, tzinfo=UTC),
            end_at=datetime(2026, 5, 20, 21, 0, tzinfo=UTC),
            description="去電影院看《奧本海默》",
            category="leisure",
        ),
    ]
    commitment = _commitment(19, 21, "看電影")
    merged = _merge_pre_commitments(llm_acts, (commitment,))
    assert len(merged) == 1
    assert merged[0].description == "去電影院看《奧本海默》"


def test_merge_splices_when_llm_missed_commitment() -> None:
    """LLM gave a generic afternoon; commitment should still appear."""
    llm_acts = [
        ScheduleActivity.create(
            start_at=datetime(2026, 5, 20, 18, 0, tzinfo=UTC),
            end_at=datetime(2026, 5, 20, 20, 0, tzinfo=UTC),
            description="晚餐",
            category="meal",
        ),
    ]
    commitment = _commitment(19, 21, "跟使用者看電影")
    merged = _merge_pre_commitments(llm_acts, (commitment,))
    descriptions = [a.description for a in merged]
    # Commitment is preserved; the overlapping LLM meal got its end
    # trimmed back to the commitment start (19:00) since commitment
    # was not fully inside it.
    assert "跟使用者看電影" in descriptions
    # Either trimmed or dropped, but cannot fully overlap.
    for a in merged:
        if a.description == "晚餐":
            assert a.end_at <= commitment.start_at


@pytest.mark.asyncio
async def test_expired_commitments_render_as_do_not_reschedule_facts() -> None:
    """CF3 P1d — dead invitations reach the planner as facts it must not use."""
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)

    await planner.plan_day(
        character=_character(),
        date_=date(2026, 5, 20),
        local_tz=UTC,
        expired_operator_commitments=(
            _expired("和使用者一起去先前說好的那家店"),
            _expired("一起看那部片", role=OPERATOR_CONFIRMED_LAPSED_ROLE),
        ),
    )

    prompt = model.last_prompt
    assert "已經過期、沒有成行的邀請／共同約定" in prompt
    assert "和使用者一起去先前說好的那家店" in prompt
    assert "一起看那部片" in prompt
    assert "2026-05-18" in prompt, "過期項要帶絕對日期，不能只有相對說法"
    # The two terminal roles read differently — pending was never agreed,
    # lapsed was agreed but never happened.
    assert "使用者始終沒有答應" in prompt
    assert "沒有留下真的一起完成的紀錄" in prompt
    # And the instruction is an outright ban, including reworded reruns.
    assert "把其中任何一項重新排進今天的行程" in prompt
    assert "換個說法" in prompt
    assert "operator_involvement 不得因此標成邀請或共同狀態" in prompt


@pytest.mark.asyncio
async def test_expired_block_absent_when_nothing_expired() -> None:
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)

    await planner.plan_day(
        character=_character(), date_=date(2026, 5, 20), local_tz=UTC,
    )

    # The standing template rule still mentions expiry; only the fact list
    # (and its header) must be gone.
    assert "事實清單，僅供你知悉" not in model.last_prompt
    assert "已經過期、沒有成行的邀請／共同約定" not in model.last_prompt


@pytest.mark.asyncio
async def test_planner_template_carries_the_generalised_expiry_rules() -> None:
    """The anti-rehatch rule must be a general principle in the template,
    present even when today happens to have no expired commitments — the
    dialogue summary can echo an old plan regardless."""
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)

    await planner.plan_day(
        character=_character(), date_=date(2026, 5, 20), local_tz=UTC,
        recent_dialogue_summary="兩人聊到之前提過的一起出門",
    )

    prompt = model.last_prompt
    assert "約定時效紀律（一）" in prompt
    assert "約定時效紀律（二）" in prompt
    assert "不是今天已成立的約定" in prompt


@pytest.mark.asyncio
async def test_existing_commitment_and_weather_discipline_do_not_regress() -> None:
    """Guard on CF3's red line: the expiry work must not weaken the
    "keep commitments verbatim" directive or the weather rules."""
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)

    await planner.plan_day(
        character=_character(),
        date_=date(2026, 5, 20),
        local_tz=UTC,
        pre_committed_activities=(_commitment(19, 21, "跟使用者看電影"),),
        expired_operator_commitments=(_expired("上週沒去成的那攤"),),
    )

    prompt = model.last_prompt
    assert "必須原封不動保留在輸出中" in prompt
    assert "天氣紀律（一）" in prompt
    assert "天氣紀律（二）" in prompt
    # Live and dead commitments stay in separate blocks — the exclusion is
    # done in code upstream, not by asking the model to ignore an input.
    assert prompt.index("已既定的承諾時段") < prompt.index("已經過期、沒有成行")


def test_merge_drops_llm_block_fully_inside_commitment() -> None:
    """LLM block wholly inside a commitment window is dropped to keep
    the commitment intact."""
    commitment = _commitment(19, 22, "跟使用者看電影")
    llm_acts = [
        ScheduleActivity.create(
            start_at=datetime(2026, 5, 20, 20, 0, tzinfo=UTC),
            end_at=datetime(2026, 5, 20, 21, 0, tzinfo=UTC),
            description="休息",
            category="rest",
        ),
    ]
    merged = _merge_pre_commitments(llm_acts, (commitment,))
    # The fully-covered LLM block: dropped because its midpoint falls
    # inside the commitment → overlap-match path keeps commitment.
    # Final list has exactly one entry: the commitment.
    assert {a.description for a in merged} == {"跟使用者看電影"}
