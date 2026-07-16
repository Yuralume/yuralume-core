"""Unit tests for the LLM-backed memory extractor."""

from collections.abc import AsyncIterator

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.json_parser import parse_memory_payload
from kokoro_link.infrastructure.memory.llm_extractor import LLMMemoryExtractor


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
        name="Airi",
        summary="溫柔",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def test_parse_memory_payload_handles_code_fence() -> None:
    raw = '```json\n[{"kind": "semantic", "content": "likes jazz", "salience": 0.8, "tags": ["music"]}]\n```'
    payloads = parse_memory_payload(raw)
    assert len(payloads) == 1
    assert payloads[0]["content"] == "likes jazz"


def test_parse_memory_payload_handles_preamble_and_trailing_text() -> None:
    raw = 'Here are the memories:\n[{"kind": "semantic", "content": "loves cats"}] cheers!'
    payloads = parse_memory_payload(raw)
    assert payloads == [{"kind": "semantic", "content": "loves cats"}]


def test_parse_memory_payload_returns_empty_when_invalid() -> None:
    assert parse_memory_payload("sorry, no memories today") == []
    assert parse_memory_payload('[not valid json') == []
    assert parse_memory_payload("{}") == []  # object, not array


def test_parse_memory_payload_handles_brackets_in_strings() -> None:
    raw = '[{"content": "quote with ]", "kind": "semantic"}]'
    payloads = parse_memory_payload(raw)
    assert payloads == [{"content": "quote with ]", "kind": "semantic"}]


@pytest.mark.asyncio
async def test_llm_extractor_builds_memory_items() -> None:
    response = (
        '[{"kind": "semantic", "content": "user lives in Tokyo", "salience": 0.9, "tags": ["location"]},'
        ' {"kind": "episodic", "content": "we talked about jazz", "salience": 0.4, "tags": []}]'
    )
    extractor = LLMMemoryExtractor(model=_ScriptedModel(response))

    items = await extractor.extract(
        character=_character(),
        conversation_id="conv-1",
        user_message="我住東京，也喜歡爵士",
        assistant_message="我們可以聊聊那個城市的爵士場景。",
    )

    assert len(items) == 2
    assert items[0].kind == MemoryKind.SEMANTIC
    assert items[0].content == "user lives in Tokyo"
    assert items[0].salience == pytest.approx(0.9)
    assert "location" in items[0].tags
    assert items[1].kind == MemoryKind.EPISODIC


@pytest.mark.asyncio
async def test_llm_extractor_drops_invalid_entries() -> None:
    response = (
        '[{"content": ""},'
        '{"kind": "unknown-kind", "content": "still counts as episodic"},'
        '{"kind": "semantic"}]'  # missing content
    )
    extractor = LLMMemoryExtractor(model=_ScriptedModel(response))

    items = await extractor.extract(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )

    assert len(items) == 1
    assert items[0].kind == MemoryKind.EPISODIC
    assert items[0].content == "still counts as episodic"


@pytest.mark.asyncio
async def test_llm_extractor_returns_empty_on_unparseable_response() -> None:
    extractor = LLMMemoryExtractor(model=_ScriptedModel("nothing worth remembering."))
    items = await extractor.extract(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hello",
    )
    assert items == []
