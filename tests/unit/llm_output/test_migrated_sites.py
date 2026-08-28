"""What the three migrated sites gained, at the sites themselves.

The differential harness proves nothing was lost. These tests pin the
other half — the widening is reachable through each site's real entry
point, not just through the layer in isolation — plus the one thing
that had to *not* widen: the tool-call site still refuses to invent a
call out of brace soup.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest

from kokoro_link.application.services.tool_call_parser import parse_tool_call
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.memory.json_parser import parse_memory_payload
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
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


# --- site 3: the post-turn five-in-one object --------------------------


@pytest.mark.asyncio
async def test_post_turn_keeps_what_arrived_before_the_cut() -> None:
    """This site asks for the largest JSON object in the codebase —
    memories, state, schedule, arc and promises in one reply — which
    makes it the one most likely to be chopped by ``max_tokens``.

    It used to drop the entire turn's extraction when that happened: no
    memories, no state delta, nothing. The fields that did arrive are
    now kept.
    """
    truncated = (
        '{"memories": ['
        '{"kind": "semantic", "content": "使用者住在東京", "salience": 0.9, "tags": ["location"]}'
        '], "state": {"emotion": "開心", "affection_delta": 3}, '
        '"schedule_adjustments": [{"action": "add", "start": "19:00"'
    )
    processor = LLMPostTurnProcessor(model=_ScriptedModel(truncated))

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="我住東京",
        assistant_message="東京的爵士場景很棒。",
    )

    assert [item.content for item in result.memories] == ["使用者住在東京"]
    assert result.state_suggestion is not None
    assert result.state_suggestion.emotion == "開心"
    assert result.state_suggestion.affection_delta == 3
    # The half-arrived adjustment is still rejected — repair recovers
    # *text*, the site's own validation decides what is usable, and an
    # ``add`` without an end time or description never was.
    assert result.schedule_adjustments == []


@pytest.mark.asyncio
async def test_post_turn_still_returns_empty_on_a_prose_reply() -> None:
    processor = LLMPostTurnProcessor(model=_ScriptedModel("今天沒什麼特別的事。"))

    result = await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="嗨",
        assistant_message="嗨！",
    )

    assert result.memories == []
    assert result.state_suggestion is None


# --- site 2: memory / schedule / weather-drift arrays ------------------


def test_memory_payload_keeps_the_entries_that_arrived() -> None:
    raw = (
        '[{"kind": "semantic", "content": "森森喜歡爵士樂"}, '
        '{"kind": "episodic", "content": "今天一起看了'
    )
    payloads = parse_memory_payload(raw)

    assert [entry["content"] for entry in payloads][0] == "森森喜歡爵士樂"


def test_memory_payload_failure_is_no_longer_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every caller's prompt demands an array — an empty one when there
    is nothing to report — so prose here is the model ignoring the
    contract, and it used to vanish without trace."""
    with caplog.at_level(
        logging.WARNING, logger="kokoro_link.infrastructure.memory.json_parser",
    ):
        assert parse_memory_payload("sorry, no memories today") == []

    assert any(
        "memory.parse_memory_payload" in record.getMessage()
        for record in caplog.records
    )


# --- site 1: chat tool calls ------------------------------------------


def test_tool_call_repairs_a_truncated_list_argument() -> None:
    """The old repair counted braces only, so a call whose arguments
    ended in an unclosed array was unrecoverable and the raw JSON blob
    went to the player."""
    raw = '{"tool": "web_search", "args": {"queries": ["夏祭", "花火'
    call = parse_tool_call(raw)

    assert call is not None
    assert call.name == "web_search"
    assert call.arguments["queries"][0] == "夏祭"


def test_tool_call_still_refuses_to_rescue_brace_soup() -> None:
    """The gate that keeps repair pointed at our own contract is policy
    and stayed at the site. Without it, a roleplay reply with a stray
    brace becomes a phantom tool call — a much worse failure than the
    leak repair exists to prevent."""
    assert parse_tool_call("今天天氣很好{但我有點累") is None
    assert parse_tool_call('{"note": "沒有 tool 欄位", "args": {"a": 1}') is None


def test_tool_call_does_not_log_on_an_ordinary_prose_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """This parser runs on *every* chat reply and most replies are
    prose. Warning on those would bury the failures worth reading."""
    with caplog.at_level(
        logging.DEBUG,
        logger="kokoro_link.application.services.tool_call_parser",
    ):
        assert parse_tool_call("查到了！今年抽選是七月一號開始。") is None

    assert caplog.records == []


def test_tool_call_logs_when_the_model_announced_a_call_and_botched_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(
        logging.WARNING,
        logger="kokoro_link.application.services.tool_call_parser",
    ):
        assert parse_tool_call('{"tool": "web_search", args: broken}') is None

    assert any(
        "chat.tool_call_parser" in record.getMessage() for record in caplog.records
    )
