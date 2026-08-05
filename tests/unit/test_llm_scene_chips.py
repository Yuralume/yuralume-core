"""In-scene action chips writer (SC1-C).

The writer's job is narrow — one JSON call, 2–3 short player moves — so
these tests are mostly about what it refuses: filler from the fake
backend, duplicates, paragraphs, and a lone suggestion (which reads as
the game telling the player what to do rather than offering a choice).

Every failure lands on the same answer, ``()``, because a missing chip
costs the player nothing: the composer is right there.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.contracts.story_scene import StorySceneChipsContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_BEAT,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.story.llm_scene_chips import (
    LLMStorySceneChipsWriter,
    NullStorySceneChipsWriter,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


class _ScriptedModel:
    provider_id = "scripted"

    def __init__(self, reply: str, *, raises: Exception | None = None) -> None:
        self._reply = reply
        self._raises = raises
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **_kwargs) -> str:  # noqa: ANN003
        self.prompts.append(prompt)
        if self._raises is not None:
            raise self._raises
        return self._reply


def _character() -> Character:
    return Character.create(
        name="Aki", summary="音樂系三年級", personality=["倔強"], interests=[],
        speaking_style="直白", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _context(
    *,
    model_reply: str | None = None,
    recent_lines: tuple[str, ...] = ("Aki：你到底想說什麼？",),
    language: str = "zh-TW",
) -> StorySceneChipsContext:
    _ = model_reply
    return StorySceneChipsContext(
        character=_character(),
        session=StorySceneSession.open_scene(
            character_id="c1",
            conversation_id="conv-1",
            source_layer=SCENE_LAYER_BEAT,
            title="把話說完",
            location="頂樓天台",
            mood="欲言又止",
            scene_type="conflict",
            dramatic_question="她要承認自己練得不夠嗎？",
            opened_at=NOW,
        ),
        recent_lines=recent_lines,
        operator_primary_language=language,
    )


async def test_returns_the_models_suggestions() -> None:
    model = _ScriptedModel(
        '{"actions": ["把話接下去", "先靠過去不說話", "轉身要走"]}',
    )
    writer = LLMStorySceneChipsWriter(model=model)

    assert await writer.suggest_actions(_context()) == (
        "把話接下去", "先靠過去不說話", "轉身要走",
    )


async def test_prompt_carries_the_scene_frame_and_the_last_exchange() -> None:
    model = _ScriptedModel('{"actions": ["a", "b"]}')
    writer = LLMStorySceneChipsWriter(model=model)

    await writer.suggest_actions(
        _context(recent_lines=("旁白：風把話吹散了。", "Aki：你到底想說什麼？")),
    )

    prompt = model.prompts[0]
    assert "頂樓天台" in prompt
    assert "欲言又止" in prompt
    assert "她要承認自己練得不夠嗎？" in prompt
    assert "Aki：你到底想說什麼？" in prompt


async def test_prompt_pins_the_operators_language() -> None:
    """Chips are player-visible text, so they follow the player's language."""
    model = _ScriptedModel('{"actions": ["a", "b"]}')
    writer = LLMStorySceneChipsWriter(model=model)

    await writer.suggest_actions(_context(language="ja-JP"))

    assert "ja-JP" in model.prompts[0]


async def test_extra_suggestions_are_capped_at_three() -> None:
    model = _ScriptedModel(
        '{"actions": ["一", "二", "三", "四", "五"]}',
    )
    writer = LLMStorySceneChipsWriter(model=model)

    assert len(await writer.suggest_actions(_context())) == 3


async def test_duplicates_do_not_fill_the_row() -> None:
    """The same chip twice reads as a bug, not as a choice."""
    model = _ScriptedModel('{"actions": ["再問一次", "再問一次", "沉默"]}')
    writer = LLMStorySceneChipsWriter(model=model)

    assert await writer.suggest_actions(_context()) == ("再問一次", "沉默")


async def test_decoration_is_stripped_so_a_chip_can_be_sent_as_is() -> None:
    model = _ScriptedModel(
        '{"actions": ["1. 「把話接下去」", "  沉默地等她  "]}',
    )
    writer = LLMStorySceneChipsWriter(model=model)

    assert await writer.suggest_actions(_context()) == (
        "把話接下去", "沉默地等她",
    )


async def test_a_single_usable_suggestion_is_no_suggestion() -> None:
    model = _ScriptedModel('{"actions": ["只有一個"]}')
    writer = LLMStorySceneChipsWriter(model=model)

    assert await writer.suggest_actions(_context()) == ()


async def test_paragraph_length_candidates_are_dropped() -> None:
    model = _ScriptedModel(
        '{"actions": ["' + "很長的一段話" * 30 + '", "短的", "也短"]}',
    )
    writer = LLMStorySceneChipsWriter(model=model)

    assert await writer.suggest_actions(_context()) == ("短的", "也短")


@pytest.mark.parametrize(
    "reply",
    [
        "完全不是 JSON",
        '{"actions": "不是陣列"}',
        "[]",
        '{"actions": []}',
    ],
)
async def test_unusable_output_degrades_to_no_chips(reply: str) -> None:
    writer = LLMStorySceneChipsWriter(model=_ScriptedModel(reply))

    assert await writer.suggest_actions(_context()) == ()


async def test_a_hung_upstream_is_bounded_rather_than_holding_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both callers hold the conversation lease while chips are written.

    An upstream that accepts the request and never answers would lock the
    player's own conversation until the lease TTL — over a suggestion
    they never needed.
    """
    import asyncio

    from kokoro_link.infrastructure.story import llm_scene_chips

    class _HangingModel:
        provider_id = "hanging"

        async def generate(self, prompt: str, **_kwargs) -> str:  # noqa: ANN003
            await asyncio.sleep(60)
            raise AssertionError("should have been cut off")

    monkeypatch.setattr(llm_scene_chips, "_TIMEOUT_SECONDS", 0.01)
    writer = LLMStorySceneChipsWriter(model=_HangingModel())

    assert await writer.suggest_actions(_context()) == ()


async def test_upstream_failure_degrades_to_no_chips() -> None:
    writer = LLMStorySceneChipsWriter(
        model=_ScriptedModel("", raises=RuntimeError("upstream down")),
    )

    assert await writer.suggest_actions(_context()) == ()


async def test_fake_backend_never_puts_filler_on_the_stage() -> None:
    class _FakeProvider:
        async def resolve(self, _feature_key, **_kwargs):  # noqa: ANN001
            return FakeChatModel(provider_id="fake")

        async def resolve_model_id(self, _feature_key, **_kwargs):  # noqa: ANN001
            return None

        async def is_fake(self, _feature_key, **_kwargs) -> bool:  # noqa: ANN001
            return True

    writer = LLMStorySceneChipsWriter(provider=_FakeProvider())

    assert await writer.suggest_actions(_context()) == ()


async def test_null_writer_is_a_real_implementation_that_answers_nothing() -> None:
    assert await NullStorySceneChipsWriter().suggest_actions(_context()) == ()
