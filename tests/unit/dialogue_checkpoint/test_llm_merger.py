"""The merger adapter and the prompt it renders.

Two things are testable without a model: what the adapter does with a
reply (including the several shapes of "nothing"), and what the prompt
actually says. The second matters more than it looks — the three red
lines (merge-don't-restate, time-neutral wording, a length ceiling) live
in the template text, and a template that silently lost one of them
would still render, still parse, and still produce a plausible summary
that quietly rots.
"""

from __future__ import annotations

import pytest

from kokoro_link.infrastructure.dialogue.llm_checkpoint_merger import (
    MAX_SUMMARY_CHARS,
    LLMDialogueCheckpointMerger,
)
from tests.unit.dialogue_checkpoint.builders import (
    at,
    character,
    conversation_of,
    user_message,
)

pytestmark = pytest.mark.asyncio


class _ScriptedModel:
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, reply: str = "合併後的摘要") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.prompts.append(prompt)
        return self.reply

    async def list_models(self) -> list[str]:
        return ["scripted"]


class _ExplodingModel(_ScriptedModel):
    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        raise RuntimeError("provider down")


def _merger(model) -> LLMDialogueCheckpointMerger:
    return LLMDialogueCheckpointMerger(model=model)


# --- the adapter -------------------------------------------------------


async def test_a_reply_becomes_the_new_summary() -> None:
    merger = _merger(_ScriptedModel("合併後的摘要"))
    result = await merger.merge(
        character=character(),
        previous_summary="舊的摘要",
        messages=conversation_of(4),
    )
    assert result.summary == "合併後的摘要"


async def test_an_empty_backlog_never_calls_the_model() -> None:
    model = _ScriptedModel()
    result = await _merger(model).merge(
        character=character(), previous_summary="舊的", messages=[],
    )
    assert result.summary == ""
    assert model.prompts == []


async def test_blank_messages_count_as_an_empty_backlog() -> None:
    model = _ScriptedModel()
    result = await _merger(model).merge(
        character=character(),
        previous_summary="",
        messages=[user_message("   ", at(10))],
    )
    assert result.summary == ""
    assert model.prompts == []


async def test_a_provider_failure_is_an_empty_result_not_an_exception() -> None:
    """The caller reads an empty summary as "keep the last-good
    checkpoint". A raise here would surface in a background post-turn as
    a crash in a subsystem that has nothing to do with the summary."""
    result = await _merger(_ExplodingModel()).merge(
        character=character(),
        previous_summary="舊的",
        messages=conversation_of(4),
    )
    assert result.summary == ""


async def test_the_no_context_marker_is_treated_as_nothing() -> None:
    """Storing 「（無明顯脈絡）」 would put those words into the
    character's prompt as if they were the memory."""
    for marker in ("（無明顯脈絡）", "(無明顯脈絡)"):
        result = await _merger(_ScriptedModel(marker)).merge(
            character=character(),
            previous_summary="",
            messages=conversation_of(4),
        )
        assert result.summary == ""


async def test_an_overlong_reply_is_truncated_to_the_ceiling() -> None:
    """Asked for in the prompt *and* enforced here — an unbounded
    cumulative summary is how compression becomes a transcript again."""
    merger = _merger(_ScriptedModel("長" * (MAX_SUMMARY_CHARS + 500)))
    result = await merger.merge(
        character=character(),
        previous_summary="",
        messages=conversation_of(4),
    )
    assert len(result.summary) == MAX_SUMMARY_CHARS


async def test_the_previous_summary_reaches_the_prompt() -> None:
    model = _ScriptedModel()
    await _merger(model).merge(
        character=character(),
        previous_summary="上一份摘要的內容",
        messages=conversation_of(4),
    )
    assert "上一份摘要的內容" in model.prompts[0]


async def test_an_absent_previous_summary_renders_a_placeholder() -> None:
    """The template must not receive an empty ``${previous_summary}``:
    a blank slot reads to the model as a missing section rather than as
    "this is the first one"."""
    model = _ScriptedModel()
    await _merger(model).merge(
        character=character(), previous_summary="", messages=conversation_of(4),
    )
    assert "（尚無）" in model.prompts[0]


async def test_the_backlog_reaches_the_prompt_oldest_first() -> None:
    model = _ScriptedModel()
    messages = conversation_of(4)
    await _merger(model).merge(
        character=character(), previous_summary="", messages=messages,
    )
    prompt = model.prompts[0]
    positions = [prompt.index(m.content[:20]) for m in messages]
    assert positions == sorted(positions)


async def test_the_model_that_served_the_call_rides_back_with_the_text() -> None:
    """The checkpoint's audit column is filled from the call that
    happened, not from whatever the configuration says later."""
    merger = _merger(_ScriptedModel("摘要"))
    result = await merger.merge(
        character=character(),
        previous_summary="",
        messages=conversation_of(4),
    )
    assert result.model == "scripted"
