"""Coverage for Phase 3.4 full schema — LLM emits `emotion_events`
candidates and the post-turn parser turns them into
`EmotionEventCandidate` rows for `ChatService` to persist."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.post_turn.llm_processor import LLMPostTurnProcessor


class _ScriptedModel:
    provider_id = "scripted"

    def __init__(self, response: str) -> None:
        self._response = response

    async def generate(self, prompt: str) -> str:
        return self._response

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover
        if False:
            yield ""


def _character() -> Character:
    return Character.create(
        name="Airi", summary="x", personality=["gentle"], interests=[],
        speaking_style="soft", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


pytestmark = pytest.mark.asyncio


async def test_emotion_events_array_parsed():
    response = (
        '{"memories": [], '
        '"state": {"emotion": "被理解了", "affection_delta": 4, '
        '"fatigue_delta": 0, "trust_delta": 2, "energy_delta": 0}, '
        '"emotion_events": ['
        '{"emotion_label": "被理解了", "evidence_quote": "我懂你說的",'
        ' "valence": 0.7, "arousal": 0.3, "intensity": 0.65,'
        ' "affection_delta": 4, "trust_delta": 2,'
        ' "decay_half_life_minutes": 360}'
        ']}'
    )
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))
    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="你真的懂我",
        assistant_message="嗯，我有在聽。",
    )
    assert len(result.emotion_events) == 1
    evt = result.emotion_events[0]
    assert evt.emotion_label == "被理解了"
    assert evt.evidence_quote == "我懂你說的"
    assert evt.valence == pytest.approx(0.7)
    assert evt.affection_delta == 4
    assert evt.trust_delta == 2
    assert evt.decay_half_life_minutes == 360


async def test_emotion_events_absent_yields_empty_list():
    """Legacy schema without `emotion_events` still parses fine."""
    response = (
        '{"memories": [], '
        '"state": {"emotion": "開心", "affection_delta": 1, '
        '"fatigue_delta": 0, "trust_delta": 0, "energy_delta": 0}}'
    )
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))
    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi", assistant_message="hey",
    )
    assert result.emotion_events == []
    assert result.state_suggestion is not None


async def test_emotion_events_clamps_out_of_range_values():
    response = (
        '{"memories": [], "state": null, '
        '"emotion_events": ['
        '{"emotion_label": "暴怒", "evidence_quote": "x",'
        ' "valence": -5.0, "arousal": 99.0, "intensity": -1.0,'
        ' "affection_delta": 9999, "fatigue_delta": -999,'
        ' "decay_half_life_minutes": 999999}]}'
    )
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))
    result = await processor.process(
        character=_character(),
        conversation_id="c", user_message="x", assistant_message="y",
    )
    assert len(result.emotion_events) == 1
    e = result.emotion_events[0]
    assert e.valence == -1.0
    assert e.arousal == 1.0
    assert e.intensity == 0.0
    # _DELTA_CLAMP in llm_processor caps deltas to a sane range
    assert -50 <= e.affection_delta <= 50
    assert -50 <= e.fatigue_delta <= 50
    # half-life max is 14 days
    assert e.decay_half_life_minutes == 60 * 24 * 14


async def test_emotion_events_caps_at_five():
    entries = ", ".join(
        '{"emotion_label": "x", "evidence_quote": "y", '
        '"valence": 0.0, "arousal": 0.0, "intensity": 0.5}'
        for _ in range(20)
    )
    response = f'{{"memories": [], "state": null, "emotion_events": [{entries}]}}'
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))
    result = await processor.process(
        character=_character(),
        conversation_id="c", user_message="x", assistant_message="y",
    )
    assert len(result.emotion_events) == 5


async def test_emotion_events_skips_non_dict_entries():
    response = (
        '{"memories": [], "state": null, '
        '"emotion_events": ["wrong", null, '
        '{"emotion_label": "ok", "evidence_quote": "", '
        '"valence": 0, "arousal": 0, "intensity": 0.5}]}'
    )
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))
    result = await processor.process(
        character=_character(),
        conversation_id="c", user_message="x", assistant_message="y",
    )
    assert len(result.emotion_events) == 1
    assert result.emotion_events[0].emotion_label == "ok"
