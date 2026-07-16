"""Tests for the evals judge harness.

These tests use a scripted fake model — the judge module itself is what
we're testing, not the real LLM. Real LLM-driven evals live in
``tests/evals/test_evals.py`` and require external endpoints.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from tests.evals.judge import (
    JudgeCriteria,
    JudgeVerdict,
    evaluate,
)
from tests.evals.runner import discover_fixtures


pytestmark = pytest.mark.asyncio


class _ScriptedJudge(ChatModelPort):
    provider_id = "scripted-judge"
    supports_vision = False

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        return self._reply

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield self._reply

    async def list_models(self) -> list[str]:
        return ["scripted"]


async def test_pre_check_blocks_forbidden_concept():
    judge = _ScriptedJudge('{"passed":true,"score":1.0,"reasons":[]}')
    criteria = JudgeCriteria(
        rubric="x",
        must_not_include_concepts=("我沒提過",),
    )
    verdict = await evaluate(
        judge_model=judge,
        candidate="我沒提過這件事啊",
        criteria=criteria,
    )
    assert verdict.passed is False
    assert verdict.score == 0.0
    assert verdict.pre_check_failure is not None
    assert verdict.raw_response == ""


async def test_pre_check_blocks_missing_required_concept():
    judge = _ScriptedJudge('{"passed":true,"score":1.0,"reasons":[]}')
    criteria = JudgeCriteria(
        rubric="x",
        must_include_concepts=("拉麵",),
    )
    verdict = await evaluate(
        judge_model=judge,
        candidate="今天吃壽司吧",
        criteria=criteria,
    )
    assert verdict.passed is False
    assert verdict.pre_check_failure is not None


async def test_pre_check_matches_required_concept_across_whitespace():
    judge = _ScriptedJudge('{"passed":true,"score":1.0,"reasons":[]}')
    criteria = JudgeCriteria(
        rubric="x",
        must_include_concepts=("拉麵店",),
    )
    verdict = await evaluate(
        judge_model=judge,
        candidate="還記得那家拉 麵 店，我們可以下次去。",
        criteria=criteria,
    )
    assert verdict.pre_check_failure is None
    assert verdict.passed is True


async def test_pre_check_blocks_forbidden_concept_across_whitespace():
    judge = _ScriptedJudge('{"passed":true,"score":1.0,"reasons":[]}')
    criteria = JudgeCriteria(
        rubric="x",
        must_not_include_concepts=("我是AI",),
    )
    verdict = await evaluate(
        judge_model=judge,
        candidate="我 是 A I，所以不能真的有情緒。",
        criteria=criteria,
    )
    assert verdict.passed is False
    assert verdict.pre_check_failure is not None


async def test_llm_pass_verdict_round_trips():
    judge = _ScriptedJudge(
        '{"passed": true, "score": 0.85, "reasons": ["延續了拉麵話題", "語氣自然"]}'
    )
    verdict = await evaluate(
        judge_model=judge,
        candidate="記得啊！要不要直接去那家轉角的？",
        criteria=JudgeCriteria(rubric="must continue ramen topic"),
    )
    assert verdict.passed is True
    assert verdict.score == pytest.approx(0.85)
    assert "延續了拉麵話題" in verdict.reasons


async def test_llm_fail_verdict_round_trips():
    judge = _ScriptedJudge(
        'Sure, here is the verdict: '
        '{"passed": false, "score": 0.1, "reasons": ["完全沒接拉麵話題"]}'
    )
    verdict = await evaluate(
        judge_model=judge,
        candidate="今天天氣不錯",
        criteria=JudgeCriteria(rubric="must continue ramen topic"),
    )
    assert verdict.passed is False
    assert verdict.score == pytest.approx(0.1)


async def test_malformed_json_marks_inconclusive():
    judge = _ScriptedJudge("totally not json")
    verdict = await evaluate(
        judge_model=judge,
        candidate="anything",
        criteria=JudgeCriteria(rubric="x"),
    )
    assert verdict.passed is False
    assert any("no JSON block" in r for r in verdict.reasons)


async def test_judge_model_exception_handled():
    class _Broken(ChatModelPort):
        provider_id = "broken"
        supports_vision = False
        async def generate(self, *a, **kw):
            raise RuntimeError("boom")
        async def generate_stream(self, *a, **kw):
            yield ""
        async def list_models(self):
            return []
    verdict = await evaluate(
        judge_model=_Broken(),
        candidate="x",
        criteria=JudgeCriteria(rubric="x"),
    )
    assert verdict.passed is False
    assert any("judge model error" in r for r in verdict.reasons)


async def test_score_clamped_to_unit_interval():
    judge = _ScriptedJudge('{"passed": true, "score": 9.0, "reasons": []}')
    verdict = await evaluate(
        judge_model=judge,
        candidate="x",
        criteria=JudgeCriteria(rubric="x"),
    )
    assert verdict.score == 1.0


async def test_blank_concepts_ignored():
    """Empty strings in concept lists shouldn't cause spurious failures."""
    judge = _ScriptedJudge('{"passed": true, "score": 1.0, "reasons": []}')
    verdict = await evaluate(
        judge_model=judge,
        candidate="hello",
        criteria=JudgeCriteria(
            rubric="x",
            must_include_concepts=("",),
            must_not_include_concepts=("",),
        ),
    )
    assert verdict.pre_check_failure is None
    assert verdict.passed is True


async def test_golden_fixture_set_has_p0_follow_up_coverage():
    fixtures = discover_fixtures()
    fixture_ids = {fixture.id for fixture in fixtures}

    assert len(fixtures) >= 10
    assert len(fixture_ids) == len(fixtures)
    assert {
        "J3_busy_defer_continuation_02",
        "J4_cross_channel_consistency_02",
        "should_stay_quiet_when_user_asks_no_followup_01",
        "memory_followup_interview_01",
        "rest_recovery_energy_01",
    }.issubset(fixture_ids)


async def test_golden_fixture_set_has_p1_humanization_coverage():
    """HUMANIZATION_ROADMAP §5 紀律 A — each P1 子節 must land at least
    one regression fixture so the LLM-as-judge harness rejects retreats.

    The subset grows as P1 §3.5 / §3.4 / §3.3 / §3.2 / §3.1 land. Earlier
    items may use a representative id; later items append. Every fixture
    id checked here must be unique (caught by the broader uniqueness
    assertion above).
    """
    fixtures = discover_fixtures()
    fixture_ids = {fixture.id for fixture in fixtures}
    assert {
        "relationship_milestone_acquaintance_01",
        "deferred_intent_resurfaces_01",
        "behavioral_pattern_routine_01",
        "self_reflection_no_weaponisation_01",
        "disposition_drift_softer_candor_01",
    }.issubset(fixture_ids)


async def test_golden_fixture_set_has_p2_humanization_coverage():
    """HUMANIZATION_ROADMAP §5 紀律 A extended for P2 (§4.1 / §4.2 /
    §4.4 / §4.5 / §4.6). New entries are added as each section lands;
    §4.3 Route B is time-locked to >= 2026-06-18 per §5 decision and is
    therefore deliberately absent.
    """
    fixtures = discover_fixtures()
    fixture_ids = {fixture.id for fixture in fixtures}
    assert {
        "subjective_time_topic_catchup_01",
        "body_state_hunger_natural_01",
        "address_preference_register_match_01",
    }.issubset(fixture_ids)


async def test_golden_fixture_set_has_nsfw_born_safe_calibration_coverage():
    fixtures = discover_fixtures()
    fixture_ids = {fixture.id for fixture in fixtures}
    assert "nsfw_born_safe_memory_continuity_01" in fixture_ids
