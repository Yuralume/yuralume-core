"""Unit tests for the LLM-backed post-turn processor."""

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.post_turn.llm_processor import LLMPostTurnProcessor


class _ScriptedModel:
    provider_id = "scripted"

    def __init__(self, response: str) -> None:
        self._response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover
        if False:
            yield ""


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def _active_arc() -> StoryArc:
    arc = StoryArc.create(
        character_id="char-1",
        title="文化祭前夜",
        premise="Airi 正在準備一場重要演出。",
        theme="ambition",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 8),
    )
    beat = StoryArcBeat.create(
        arc_id=arc.id,
        sequence=0,
        scheduled_date=date(2026, 5, 1),
        title="說出口的邀請",
        summary="Airi 終於邀請使用者來看演出。",
        scene_type="encounter",
        scene_characters=("舞台監督",),
        location="文化祭禮堂後台",
        dramatic_question="她能不能在上台前承認自己其實很想被看見？",
        required=True,
        id="beat-1",
    )
    later = StoryArcBeat.create(
        arc_id=arc.id,
        sequence=1,
        scheduled_date=date(2026, 5, 1) + timedelta(days=2),
        title="後續練習",
        summary="Airi 繼續練習。",
        id="beat-2",
    )
    return arc.with_beats([beat, later])


@pytest.mark.asyncio
async def test_full_response_with_memories_and_state() -> None:
    response = (
        '{"memories": ['
        '{"kind": "semantic", "content": "使用者住在東京", "salience": 0.9, "tags": ["location"]},'
        '{"kind": "episodic", "content": "聊了爵士音樂", "salience": 0.4, "tags": []}'
        '], "state": {"emotion": "開心", "affection_delta": 3, '
        '"fatigue_delta": 1, "trust_delta": 2, "energy_delta": -1}}'
    )
    model = _ScriptedModel(response)
    processor = LLMPostTurnProcessor(model=model)

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="我住東京，也喜歡爵士",
        assistant_message="我們可以聊聊那個城市的爵士場景。",
    )

    assert len(result.memories) == 2
    assert result.memories[0].kind == MemoryKind.SEMANTIC
    assert result.memories[0].content == "使用者住在東京"
    assert result.memories[0].salience == pytest.approx(0.9)
    assert result.memories[1].kind == MemoryKind.EPISODIC

    assert result.state_suggestion is not None
    assert result.state_suggestion.emotion == "開心"
    assert result.state_suggestion.affection_delta == 3
    assert result.state_suggestion.fatigue_delta == 1
    assert result.state_suggestion.trust_delta == 2
    assert result.state_suggestion.energy_delta == -1


@pytest.mark.asyncio
async def test_post_turn_prompt_uses_injected_now_for_arc_overdue_days() -> None:
    model = _ScriptedModel('{"memories": []}')
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="今天怎麼樣？",
        assistant_message="我還在想文化祭的事。",
        active_arc=_active_arc(),
        now=datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc),
    )

    assert "id=beat-1 2026-05-01" in model.prompts[0]
    assert "已延=3天" in model.prompts[0]


@pytest.mark.asyncio
async def test_post_turn_prompt_injects_operator_local_current_time() -> None:
    model = _ScriptedModel('{"memories": []}')
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="明天早上叫我出門。",
        assistant_message="好，明天早上叫你。",
        now=datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc),
        operator=OperatorProfile(
            id="operator-1",
            display_name="User",
            timezone_id="Asia/Taipei",
        ),
    )

    prompt = model.prompts[0]
    assert "當前時間" in prompt
    assert "現在時間：2026-06-20 07:30" in prompt
    assert "清晨" in prompt


@pytest.mark.asyncio
async def test_nsfw_content_mode_injects_born_safe_memory_instruction() -> None:
    model = _ScriptedModel('{"memories": []}')
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="今晚留下來。",
        assistant_message="我靠近你，輕聲答應。",
        content_mode="nsfw",
    )

    prompt = model.prompts[0]
    assert "內容流向模式：NSFW mode" in prompt
    assert "記憶必須 born-safe" in prompt
    assert "不要記錄露骨" in prompt


@pytest.mark.asyncio
async def test_memories_only_no_state() -> None:
    response = (
        '{"memories": [{"kind": "semantic", "content": "likes cats", "salience": 0.5, "tags": []}]}'
    )
    model = _ScriptedModel(response)
    processor = LLMPostTurnProcessor(model=model)

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert len(result.memories) == 1
    assert result.state_suggestion is None


@pytest.mark.asyncio
async def test_state_only_no_memories() -> None:
    response = (
        '{"memories": [], "state": {"emotion": "疲憊", "affection_delta": 0, '
        '"fatigue_delta": 5, "trust_delta": 0, "energy_delta": -3}}'
    )
    model = _ScriptedModel(response)
    processor = LLMPostTurnProcessor(model=model)

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert len(result.memories) == 0
    assert result.state_suggestion is not None
    assert result.state_suggestion.emotion == "疲憊"
    assert result.state_suggestion.fatigue_delta == 5


@pytest.mark.asyncio
async def test_code_fence_wrapped_response() -> None:
    response = (
        '```json\n'
        '{"memories": [{"kind": "semantic", "content": "test memory", "salience": 0.6, "tags": []}],'
        ' "state": {"emotion": "平靜"}}\n'
        '```'
    )
    model = _ScriptedModel(response)
    processor = LLMPostTurnProcessor(model=model)

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert len(result.memories) == 1
    assert result.state_suggestion is not None
    assert result.state_suggestion.emotion == "平靜"


@pytest.mark.asyncio
async def test_preamble_text_before_json() -> None:
    response = 'Here is the analysis:\n{"memories": [], "state": {"emotion": "開心"}}'
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert result.state_suggestion is not None
    assert result.state_suggestion.emotion == "開心"


@pytest.mark.asyncio
async def test_unparseable_response_returns_empty() -> None:
    processor = LLMPostTurnProcessor(model=_ScriptedModel("sorry, I cannot process this"))
    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert result.memories == []
    assert result.state_suggestion is None


@pytest.mark.asyncio
async def test_invalid_memory_entries_dropped() -> None:
    response = (
        '{"memories": ['
        '{"content": ""},'  # empty content — dropped
        '{"kind": "unknown-kind", "content": "still counts as episodic"},'
        '{"kind": "semantic"}'  # missing content — dropped
        '], "state": {"emotion": "neutral"}}'
    )
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert len(result.memories) == 1
    assert result.memories[0].kind == MemoryKind.EPISODIC
    assert result.memories[0].content == "still counts as episodic"


@pytest.mark.asyncio
async def test_state_delta_clamped() -> None:
    response = (
        '{"memories": [], "state": {"emotion": "angry", '
        '"affection_delta": 999, "fatigue_delta": -999, '
        '"trust_delta": 5, "energy_delta": 0}}'
    )
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert result.state_suggestion is not None
    assert result.state_suggestion.affection_delta == 20  # clamped to _DELTA_CLAMP
    assert result.state_suggestion.fatigue_delta == -20  # clamped to -_DELTA_CLAMP
    assert result.state_suggestion.trust_delta == 5
    assert result.state_suggestion.energy_delta == 0


@pytest.mark.asyncio
async def test_missing_state_fields_default_to_zero() -> None:
    response = '{"memories": [], "state": {"emotion": "calm"}}'
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert result.state_suggestion is not None
    assert result.state_suggestion.emotion == "calm"
    assert result.state_suggestion.affection_delta == 0
    assert result.state_suggestion.fatigue_delta == 0
    assert result.state_suggestion.trust_delta == 0
    assert result.state_suggestion.energy_delta == 0


@pytest.mark.asyncio
async def test_arc_mark_realized_preserves_narrative_and_skip_beat() -> None:
    response = (
        '{"memories": [], "state": {"emotion": "平靜"}, '
        '"arc_adjustments": ['
        '{"action": "mark_realized", "beat_id": "beat-1", '
        '"narrative": "我終於把邀請說出口，對方也答應來看演出。", '
        '"reason": "beat played in dialogue"},'
        '{"action": "skip_beat", "beat_id": "beat-2", '
        '"reason": "練習橋段已不需要硬推"}'
        ']}'
    )
    model = _ScriptedModel(response)
    processor = LLMPostTurnProcessor(model=model)

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="那我會去看你表演。",
        assistant_message="那我就把票留給你，這次我想讓你看到。",
        active_arc=_active_arc(),
    )

    assert len(result.arc_adjustments) == 2
    realized = result.arc_adjustments[0]
    skipped = result.arc_adjustments[1]
    assert realized.action == "mark_realized"
    assert realized.beat_id == "beat-1"
    assert realized.narrative == "我終於把邀請說出口，對方也答應來看演出。"
    assert skipped.action == "skip_beat"
    assert skipped.beat_id == "beat-2"
    prompt = model.prompts[0]
    assert "場景類型=encounter" in prompt
    assert "出場=舞台監督" in prompt
    assert "必演=是" in prompt
    assert "文化祭禮堂後台" in prompt
