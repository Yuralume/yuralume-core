from __future__ import annotations

from datetime import date

import pytest

from kokoro_link.contracts.story_arc import ArcCompletionMemoryContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    BEAT_REALIZED,
    StoryArc,
    StoryArcBeat,
    TENSION_CLIMAX,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.story.llm_arc_completion_memory_writer import (
    LLMArcCompletionMemoryWriter,
    NullArcCompletionMemoryWriter,
)


class _ScriptedModel:
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs):  # noqa: ANN003
        self.prompts.append(prompt)
        return self.response

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        yield await self.generate(prompt, **kwargs)

    async def list_models(self) -> list[str]:
        return ["scripted"]


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="想成為演員的學生。",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _context() -> ArcCompletionMemoryContext:
    character = _character()
    today = date(2026, 6, 1)
    arc = StoryArc.create(
        character_id=character.id,
        title="試鏡週",
        premise="她面對重要試鏡。",
        theme="ambition",
        start_date=today,
        end_date=today,
    )
    beat = StoryArcBeat.create(
        arc_id=arc.id,
        sequence=0,
        scheduled_date=today,
        title="登台",
        summary="她在燈下沒有逃走。",
        tension=TENSION_CLIMAX,
        status=BEAT_REALIZED,
    )
    return ArcCompletionMemoryContext(
        character=character,
        arc=arc.with_beats([beat]),
        realized_beats=(beat,),
    )


@pytest.mark.asyncio
async def test_completion_writer_parses_json_content_and_renders_beats() -> None:
    model = _ScriptedModel(
        '{"content":"我們記得試鏡週收束時，她在燈下沒有逃走。"}',
    )
    writer = LLMArcCompletionMemoryWriter(model=model)

    draft = await writer.write_memory(_context())

    assert draft.content == "我們記得試鏡週收束時，她在燈下沒有逃走。"
    prompt = model.prompts[0]
    assert "試鏡週" in prompt
    assert "登台" in prompt
    assert "只寫 1 句" in prompt


@pytest.mark.asyncio
async def test_completion_writer_falls_back_on_bad_json() -> None:
    writer = LLMArcCompletionMemoryWriter(model=_ScriptedModel("{bad"))

    draft = await writer.write_memory(_context())

    assert "我們一起走完了《試鏡週》" in draft.content


@pytest.mark.asyncio
async def test_null_completion_writer_is_deterministic() -> None:
    draft = await NullArcCompletionMemoryWriter().write_memory(_context())

    assert "登台：她在燈下沒有逃走" in draft.content


def _has_han(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _has_kana(text: str) -> bool:
    return any("぀" <= ch <= "ヿ" for ch in text)


@pytest.mark.asyncio
async def test_null_completion_writer_localizes_to_english() -> None:
    """The deterministic fallback (LLM-free path, used both directly and
    by ``LLMArcCompletionMemoryWriter`` on failure) hardcoded a zh-TW
    wrapper template ("我們一起走完了《…》：…") regardless of
    ``operator_primary_language``. The arc title / beat summary are the
    story's own (possibly Chinese) content, not template text, so we
    only assert the wrapper phrase itself localized."""
    from dataclasses import replace

    context = replace(_context(), operator_primary_language="en-US")

    draft = await NullArcCompletionMemoryWriter().write_memory(context)

    assert "we finished" in draft.content.lower()
    assert "together" in draft.content.lower()
    assert "我們一起走完了" not in draft.content


@pytest.mark.asyncio
async def test_null_completion_writer_localizes_to_japanese() -> None:
    from dataclasses import replace

    context = replace(_context(), operator_primary_language="ja-JP")

    draft = await NullArcCompletionMemoryWriter().write_memory(context)

    assert "私たちは一緒に" in draft.content
    assert "を歩みきった" in draft.content


@pytest.mark.asyncio
async def test_completion_writer_falls_back_localized_on_bad_json() -> None:
    from dataclasses import replace

    context = replace(_context(), operator_primary_language="en-US")
    writer = LLMArcCompletionMemoryWriter(model=_ScriptedModel("{bad"))

    draft = await writer.write_memory(context)

    assert "we finished" in draft.content.lower()
    assert "我們一起走完了" not in draft.content
