"""LLMSchedulePlanner: pre_committed_activities prompt + merge."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
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
    """Even when LLM call crashes, seed commitments survive (so they
    get re-fed to the next plan_day attempt via ensure_schedule)."""

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
    assert result.is_planned is False
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
