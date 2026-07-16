"""Phase 3 assertions: encounter transcript/summary quality gates
(ENCOUNTER_CHAT_PARITY_PLAN) — proactive/feed-style whole-output gating,
retry-once with feedback, and hard fail-open guarantees.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterRunner,
    EncounterBeat,
    EncounterReflection,
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


def _runner(gate, *, provider=None, history=()) -> CharacterEncounterRunner:
    return CharacterEncounterRunner(
        encounter_repository=_HistoryRepo(history),
        character_repository=MagicMock(),
        memory_writer=MagicMock(),
        relationship_service=MagicMock(),
        provider=provider or _Provider(),
        local_tz=timezone.utc,
        novelty_gate=gate,
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
    assert gate.characters[0] is char_a


@pytest.mark.asyncio
async def test_transcript_gate_failure_regenerates_once_with_feedback() -> None:
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
    # Regenerated transcript replaces the gated one.
    assert any("河堤" in line.text for line in result)
    # The retry prompt carries the gate feedback.
    assert any("和昨天的碰面內容幾乎一樣" in p for p in provider.model.prompts)
    # Retry-once: the gate is not re-evaluated after regeneration.
    assert len(gate.contexts) == 1


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
    # Re-reflect prompt carried the feedback.
    assert any("摘要與昨天雷同" in p for p in provider.model.prompts)


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
