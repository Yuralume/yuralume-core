"""Phase 3 assertions: encounter transcript/summary quality gates
(ENCOUNTER_CHAT_PARITY_PLAN, QG6) — proactive/feed-style whole-output
gating via the shared output-quality orchestrator (QG0): soft failures
stay best-effort (regenerate once, ship whichever draft is better, keep
the original when regeneration itself fails), while a hard failure that
survives its regeneration now raises instead of quietly shipping a
defect — the caller's ``run()`` turns that into a failed encounter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterRunner,
    EncounterBeat,
    EncounterOutputUnusable,
    EncounterReflection,
)
from kokoro_link.application.services.output_quality import (
    OutputQualityOrchestrator,
)
from kokoro_link.contracts.novelty_gate import NoveltyVerdict
from kokoro_link.domain.entities.character_encounter import EncounterLine

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


class _FakeModel:
    def __init__(self, response: str) -> None:
        self.prompts: list[str] = []
        self._response = response

    async def generate(self, prompt: str, *, model: str | None = None) -> str:
        self.prompts.append(prompt)
        return self._response


class _Provider:
    def __init__(self, response: str = "新的一句台詞") -> None:
        self.model = _FakeModel(response)

    async def is_fake(self, feature_key=None, *, character=None) -> bool:
        return False

    async def resolve(self, feature_key=None, *, character=None):
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return None


class _Gate:
    def __init__(self, verdicts) -> None:
        self._verdicts = list(verdicts)
        self.contexts = []
        self.characters = []

    async def evaluate(self, context, *, character=None):
        self.contexts.append(context)
        self.characters.append(character)
        if not self._verdicts:
            return NoveltyVerdict(passes=True)
        result = self._verdicts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _HistoryRepo:
    def __init__(self, items=()) -> None:
        self._items = list(items)

    async def list_for_relationship(self, relationship_id, *, limit=30):
        return list(self._items)[:limit]


def _char(cid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=cid, name=cid.upper(), summary=f"{cid} summary", user_id="u1",
        personality=(), speaking_style="", interests=(), boundaries=(),
    )


def _encounter() -> SimpleNamespace:
    return SimpleNamespace(
        id="enc-now",
        relationship_id="rel-1",
        location="神社前庭",
        trigger_reason="路過打招呼",
        max_turns=2,
        scheduled_for=_NOW,
    )


def _old_encounter() -> SimpleNamespace:
    return SimpleNamespace(
        id="enc-old",
        relationship_id="rel-1",
        status="completed",
        scheduled_for=_NOW - timedelta(days=1),
        summary_for_a="聊到亮亮的東西",
        summary_for_b="被拉去看亮亮的東西",
        trigger_reason="路過",
    )


def _transcript(char_a, char_b) -> tuple[EncounterLine, ...]:
    return (
        EncounterLine(speaker_character_id=char_a.id, text="又看到亮亮的東西了"),
        EncounterLine(speaker_character_id=char_b.id, text="又來？"),
    )


def _runner(
    gate,
    *,
    provider=None,
    history=(),
    max_retries: int = 1,
    orchestrator: OutputQualityOrchestrator | None | bool = False,
) -> CharacterEncounterRunner:
    """Build a runner wired the way the container wires it: the same gate
    object backs both ``reply_quality_gate`` and the orchestrator's
    internal gate (bootstrap/container.py QG0), so a fake gate's queued
    verdicts drive the whole review→regenerate→dispose band.

    ``orchestrator=False`` (the default) builds one from *gate*.
    ``orchestrator=None`` wires the runner with no orchestrator at all —
    for the one test that exercises that degrade path explicitly.
    """
    resolved_orchestrator = (
        OutputQualityOrchestrator(gate=gate) if orchestrator is False
        else orchestrator
    )
    return CharacterEncounterRunner(
        encounter_repository=_HistoryRepo(history),
        character_repository=MagicMock(),
        memory_writer=MagicMock(),
        relationship_service=MagicMock(),
        provider=provider or _Provider(),
        local_tz=timezone.utc,
        reply_quality_gate=gate,
        reply_quality_gate_max_retries=max_retries,
        output_quality_orchestrator=resolved_orchestrator,
    )


@pytest.mark.asyncio
async def test_transcript_gate_pass_keeps_original() -> None:
    gate = _Gate([NoveltyVerdict(passes=True)])
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate)
    original = _transcript(char_a, char_b)
    result = await runner._gate_transcript(
        _encounter(), char_a, char_b, original,
        speaker_contexts={"a": [], "b": []}, beats=(),
        register_profile=None, language="zh-TW", now=_NOW,
    )
    assert result == original
    context = gate.contexts[0]
    assert "又看到亮亮的東西了" in context.response_text
    assert "碰面" in context.latest_user_message
    assert context.operator_primary_language == "zh-TW"
    assert gate.characters[0] is char_a


@pytest.mark.asyncio
async def test_transcript_gate_soft_failure_regenerates_and_reviews_again() -> None:
    """QG6: unlike the pre-QG0 hand-rolled gate, a background surface's
    regenerated draft is re-reviewed — the orchestrator's whole point is
    that "regenerate once" is a real second check, not a ritual."""
    gate = _Gate([
        NoveltyVerdict(passes=False, lacks_novelty=True,
                       feedback="和昨天的碰面內容幾乎一樣"),
    ])
    provider = _Provider("聊點別的吧，我今天去了河堤")
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, provider=provider, history=[_old_encounter()])
    result = await runner._gate_transcript(
        _encounter(), char_a, char_b, _transcript(char_a, char_b),
        speaker_contexts={"a": [], "b": []},
        beats=(EncounterBeat(topic="河堤拍照"),),
        register_profile=None, language="zh-TW", now=_NOW,
    )
    # Regenerated transcript replaces the gated one — the queued gate has
    # only one failing verdict, so the re-review of the regen auto-passes.
    assert any("河堤" in line.text for line in result)
    # The retry prompt carries the gate feedback.
    assert any("和昨天的碰面內容幾乎一樣" in p for p in provider.model.prompts)
    # Background policy re-reviews the regenerated draft.
    assert len(gate.contexts) == 2


@pytest.mark.asyncio
async def test_transcript_gate_hard_failure_recovers_on_clean_regen() -> None:
    gate = _Gate([
        NoveltyVerdict(passes=False, structural_leak=True, feedback="洩漏了 JSON 標籤"),
        NoveltyVerdict(passes=True),
    ])
    provider = _Provider("乾淨的新台詞，沒有標籤")
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, provider=provider, history=[_old_encounter()])
    result = await runner._gate_transcript(
        _encounter(), char_a, char_b, _transcript(char_a, char_b),
        speaker_contexts={"a": [], "b": []},
        beats=(EncounterBeat(topic="河堤拍照"),),
        register_profile=None, language="zh-TW", now=_NOW,
    )
    assert any("乾淨的新台詞" in line.text for line in result)
    assert len(gate.contexts) == 2


@pytest.mark.asyncio
async def test_transcript_gate_hard_failure_raises_after_failed_regen() -> None:
    """The 2026-08-26-shaped defect: a hard axis (structural leak, here)
    fires again on the regenerated draft. QG6 fail-closes — the caller
    must not ship it — by raising rather than falling back to the
    pre-gate original."""
    gate = _Gate([
        NoveltyVerdict(passes=False, structural_leak=True, feedback="洩漏了 JSON 標籤"),
        NoveltyVerdict(passes=False, structural_leak=True, feedback="重生後仍然洩漏"),
    ])
    provider = _Provider("還是漏了標籤")
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, provider=provider, history=[_old_encounter()])
    with pytest.raises(EncounterOutputUnusable):
        await runner._gate_transcript(
            _encounter(), char_a, char_b, _transcript(char_a, char_b),
            speaker_contexts={"a": [], "b": []},
            beats=(EncounterBeat(topic="河堤拍照"),),
            register_profile=None, language="zh-TW", now=_NOW,
        )
    assert len(gate.contexts) == 2


@pytest.mark.asyncio
async def test_transcript_gate_hard_failure_raises_when_retries_disabled() -> None:
    """Zero retries used to mean "review only, keep the original" for
    every failure. QG6 keeps that for soft failures but not hard ones —
    see the sibling soft-failure test below."""
    gate = _Gate([
        NoveltyVerdict(passes=False, structural_leak=True, feedback="洩漏了 JSON 標籤"),
    ])
    provider = _Provider()
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, provider=provider, history=[_old_encounter()], max_retries=0)
    with pytest.raises(EncounterOutputUnusable):
        await runner._gate_transcript(
            _encounter(), char_a, char_b, _transcript(char_a, char_b),
            speaker_contexts={"a": [], "b": []}, beats=(),
            register_profile=None, language="zh-TW", now=_NOW,
        )
    assert provider.model.prompts == []  # no regeneration attempted


@pytest.mark.asyncio
async def test_transcript_gate_soft_failure_keeps_original_when_retries_disabled() -> None:
    gate = _Gate([
        NoveltyVerdict(passes=False, lacks_novelty=True, feedback="流於形式"),
    ])
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, max_retries=0)
    original = _transcript(char_a, char_b)
    result = await runner._gate_transcript(
        _encounter(), char_a, char_b, original,
        speaker_contexts={"a": [], "b": []}, beats=(),
        register_profile=None, language="zh-TW", now=_NOW,
    )
    assert result == original


@pytest.mark.asyncio
async def test_transcript_gate_fail_open_on_judge_error() -> None:
    gate = _Gate([RuntimeError("judge exploded")])
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate)
    original = _transcript(char_a, char_b)
    result = await runner._gate_transcript(
        _encounter(), char_a, char_b, original,
        speaker_contexts={"a": [], "b": []}, beats=(),
        register_profile=None, language="zh-TW", now=_NOW,
    )
    assert result == original


@pytest.mark.asyncio
async def test_transcript_gate_skipped_without_gate_wired() -> None:
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(None)
    original = _transcript(char_a, char_b)
    result = await runner._gate_transcript(
        _encounter(), char_a, char_b, original,
        speaker_contexts={"a": [], "b": []}, beats=(),
        register_profile=None, language="zh-TW", now=_NOW,
    )
    assert result == original


@pytest.mark.asyncio
async def test_transcript_gate_skipped_without_orchestrator_wired() -> None:
    """A gate wired without its orchestrator counterpart (a container
    mis-wiring, or a caller that predates QG6) degrades to "no gate" —
    same as before, not a crash."""
    gate = _Gate([NoveltyVerdict(passes=False, structural_leak=True)])
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, orchestrator=None)
    original = _transcript(char_a, char_b)
    result = await runner._gate_transcript(
        _encounter(), char_a, char_b, original,
        speaker_contexts={"a": [], "b": []}, beats=(),
        register_profile=None, language="zh-TW", now=_NOW,
    )
    assert result == original
    assert gate.contexts == []


@pytest.mark.asyncio
async def test_summary_gate_first_meetup_always_passes() -> None:
    gate = _Gate([NoveltyVerdict(passes=False, lacks_novelty=True)])
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, history=[])
    reflection = EncounterReflection(
        summary_for_a="聊到亮亮的東西", summary_for_b="聊到亮亮的東西",
    )
    result = await runner._gate_reflection(
        _encounter(), char_a, char_b, _transcript(char_a, char_b), reflection,
        speaker_contexts={"a": [], "b": []}, language="zh-TW", now=_NOW,
    )
    assert result is reflection
    assert gate.contexts == []  # no history → gate not even consulted


@pytest.mark.asyncio
async def test_summary_gate_repetition_triggers_re_reflect() -> None:
    gate = _Gate([
        NoveltyVerdict(passes=False, lacks_novelty=True,
                       feedback="摘要與昨天雷同"),
    ])
    provider = _Provider(
        '{"summary_for_a": "這次聊了河堤拍照的成果", '
        '"summary_for_b": "看了A拍的照片"}',
    )
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, provider=provider, history=[_old_encounter()])
    reflection = EncounterReflection(
        summary_for_a="又聊到亮亮的東西", summary_for_b="又聊到亮亮的東西",
    )
    result = await runner._gate_reflection(
        _encounter(), char_a, char_b, _transcript(char_a, char_b), reflection,
        speaker_contexts={"a": [], "b": []}, language="zh-TW", now=_NOW,
    )
    assert result.summary_for_a == "這次聊了河堤拍照的成果"
    # Known material carried the previous summaries for comparison.
    assert any("亮亮的東西" in line for line in gate.contexts[0].known_material)
    assert gate.contexts[0].operator_primary_language == "zh-TW"
    # Re-reflect prompt carried the feedback.
    assert any("摘要與昨天雷同" in p for p in provider.model.prompts)
    # Background policy re-reviews the regenerated summary too.
    assert len(gate.contexts) == 2


@pytest.mark.asyncio
async def test_summary_gate_hard_failure_raises_after_failed_regen() -> None:
    gate = _Gate([
        NoveltyVerdict(passes=False, language_mismatch=True, feedback="語言不符玩家設定"),
        NoveltyVerdict(passes=False, language_mismatch=True, feedback="重生後仍然不符"),
    ])
    provider = _Provider(
        '{"summary_for_a": "still english", "summary_for_b": "still english"}',
    )
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, provider=provider, history=[_old_encounter()])
    reflection = EncounterReflection(summary_for_a="x", summary_for_b="y")
    with pytest.raises(EncounterOutputUnusable):
        await runner._gate_reflection(
            _encounter(), char_a, char_b, _transcript(char_a, char_b), reflection,
            speaker_contexts={"a": [], "b": []}, language="zh-TW", now=_NOW,
        )
    assert len(gate.contexts) == 2


@pytest.mark.asyncio
async def test_summary_gate_fail_open_on_judge_error() -> None:
    gate = _Gate([RuntimeError("judge exploded")])
    char_a, char_b = _char("a"), _char("b")
    runner = _runner(gate, history=[_old_encounter()])
    reflection = EncounterReflection(summary_for_a="x", summary_for_b="y")
    result = await runner._gate_reflection(
        _encounter(), char_a, char_b, _transcript(char_a, char_b), reflection,
        speaker_contexts={"a": [], "b": []}, language="zh-TW", now=_NOW,
    )
    assert result is reflection
