"""The 起幕 opening writer (SC1-A).

The behaviour worth pinning is the *refusal*: unlike every other story
writer in this package, this one has no synthesized fallback. The opening
is what a hosted player is charged a fixed price for, so a fake provider,
a crashed call, unparseable output or half an opening must all come back
as "no opening" rather than as scaffolding the player pays for.
"""

from __future__ import annotations

from datetime import date

import pytest

from kokoro_link.contracts.story_scene import (
    StorySceneMaterial,
    StorySceneOpeningContext,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_scene_session import SCENE_LAYER_BEAT
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.story.llm_scene_opener import (
    LLMStorySceneOpener,
    NullStorySceneOpener,
)

pytestmark = pytest.mark.asyncio

TODAY = date(2026, 6, 1)

_GOOD_JSON = """
Sure, here you go:
{
  "narration": "頂樓的風把譜架吹得直響，她已經站在那裡很久了。",
  "character_line": "……你來了。我以為今天只有我一個人。",
  "title": "頂樓的獨奏",
  "location": "頂樓天台",
  "mood": "欲言又止"
}
"""


class _ScriptedModel(FakeChatModel):
    def __init__(self, reply: str | None = None, *, raises: Exception | None = None) -> None:
        super().__init__("scripted")
        self._reply = reply
        self._raises = raises
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN003
        self.prompts.append(prompt)
        if self._raises is not None:
            raise self._raises
        return self._reply or ""


def _character() -> Character:
    return Character(
        id="c1",
        name="Mio",
        summary="a violinist",
        personality=(),
        interests=(),
        speaking_style="soft",
        boundaries=(),
        aspirations=(),
        appearance="",
        world_frame="modern",
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _context(
    *,
    operator_position: str | None = None,
    operator_note: str | None = None,
) -> StorySceneOpeningContext:
    return StorySceneOpeningContext(
        character=_character(),
        material=StorySceneMaterial(
            layer=SCENE_LAYER_BEAT,
            title="頂樓的告白",
            summary="她終於把話說出口",
            arc_id="arc-1",
            beat_id="beat-1",
            arc_title="夏日的獨奏會",
            arc_premise="她要準備一場獨奏會",
            arc_tone="daily",
            tension="rising",
            scene_type="encounter",
            location="頂樓",
            dramatic_question="她會說出口嗎？",
            scene_characters=("學姊",),
            operator_position=operator_position,
            operator_note=operator_note,
        ),
        today=TODAY,
        operator_primary_language="ja-JP",
    )


def _opener(model) -> LLMStorySceneOpener:  # noqa: ANN001
    return LLMStorySceneOpener(model=model)


async def test_narration_keeps_paragraph_breaks() -> None:
    """The opening is interactive fiction, not a stage direction: the
    narration arrives in paragraphs and must reach the frontend with its
    blank-line separators intact (SceneFrame splits on them). Whitespace
    *inside* a paragraph still collapses."""
    reply = (
        '{"narration": "第一段開場。\\n\\n第二段有\\t多餘  空白。\\n\\n\\n\\n第三段。",'
        ' "character_line": "……你來了。"}'
    )

    draft = await _opener(_ScriptedModel(reply)).write_opening(_context())

    assert draft is not None
    assert draft.narration == "第一段開場。\n\n第二段有 多餘 空白。\n\n第三段。"


async def test_literal_newlines_inside_the_json_string_still_parse() -> None:
    """Models asked for multi-paragraph prose often emit *literal*
    newlines inside the JSON string instead of \\n escapes. Strict JSON
    rejects those control characters, and a failed parse here fails the
    whole paid action — so the parser must tolerate them."""
    reply = '{"narration": "第一段。\n\n第二段。", "character_line": "好。"}'

    draft = await _opener(_ScriptedModel(reply)).write_opening(_context())

    assert draft is not None
    assert draft.narration == "第一段。\n\n第二段。"


async def test_parses_a_well_formed_opening() -> None:
    opener = _opener(_ScriptedModel(_GOOD_JSON))

    draft = await opener.write_opening(_context())

    assert draft is not None
    assert draft.narration.startswith("頂樓的風")
    assert draft.character_line.startswith("……你來了")
    assert draft.title == "頂樓的獨奏"
    assert draft.location == "頂樓天台"
    assert draft.mood == "欲言又止"
    # SC1-C owns the chips; the opener leaves the slot empty
    assert draft.suggested_actions == ()


async def test_prompt_carries_the_material_and_the_operator_language() -> None:
    model = _ScriptedModel(_GOOD_JSON)

    await _opener(model).write_opening(_context())

    prompt = model.prompts[0]
    assert "ja-JP" in prompt
    assert "頂樓的告白" in prompt
    assert "她會說出口嗎？" in prompt
    assert "學姊" in prompt
    assert "夏日的獨奏會" in prompt
    # the absolute-date discipline block travels with every story prompt
    assert TODAY.isoformat() in prompt


# ── OP2-D: player position framing ───────────────────────────────────


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        ("central", "核心"),
        ("present", "在場，但不是核心"),
        ("absent", "原本不在場"),
    ],
    ids=["central", "present", "absent"],
)
async def test_each_known_position_gets_its_own_opening_framing(
    position: str, expected: str,
) -> None:
    model = _ScriptedModel(_GOOD_JSON)

    await _opener(model).write_opening(
        _context(operator_position=position),
    )

    assert expected in model.prompts[0]


async def test_an_unjudged_position_asks_the_model_to_derive_one() -> None:
    """§2 pick #4 — no author judged this beat, so the prompt hands the
    judgment to the model instead of guessing a position in code."""
    model = _ScriptedModel(_GOOD_JSON)

    await _opener(model).write_opening(_context(operator_position=None))

    assert "沒有人標記過" in model.prompts[0]


async def test_the_operator_note_reaches_the_opening_prompt() -> None:
    model = _ScriptedModel(_GOOD_JSON)

    await _opener(model).write_opening(
        _context(operator_position="central", operator_note="她要向你坦白"),
    )

    assert "她要向你坦白" in model.prompts[0]


async def test_missing_frame_labels_fall_back_instead_of_failing() -> None:
    """Decoration degrades; the scene still opens."""
    model = _ScriptedModel(
        '{"narration": "旁白。", "character_line": "第一句。"}',
    )

    draft = await _opener(model).write_opening(_context())

    assert draft is not None
    assert draft.title == ""
    # falls back to the material's own location rather than inventing one
    assert draft.location == "頂樓"
    assert draft.mood is None


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "sorry, I can't do that",
        "{not json at all",
        '["a list, not an object"]',
        '{"narration": "旁白但沒有台詞。"}',
        '{"character_line": "台詞但沒有旁白。"}',
        '{"narration": "  ", "character_line": "  "}',
    ],
    ids=[
        "empty", "prose", "broken-json", "wrong-type",
        "no-line", "no-narration", "blank",
    ],
)
async def test_an_unusable_reply_is_a_failed_opening(reply: str) -> None:
    assert await _opener(_ScriptedModel(reply)).write_opening(_context()) is None


async def test_a_crashing_call_is_a_failed_opening() -> None:
    model = _ScriptedModel(raises=RuntimeError("upstream exploded"))

    assert await _opener(model).write_opening(_context()) is None


async def test_a_fake_provider_refuses_rather_than_synthesizing() -> None:
    """FakeChatModel is an *unconfigured* backend, not a degraded one —
    its output must never reach the player's stage, least of all on a
    paid action."""
    model = FakeChatModel("fake")

    draft = await _opener(model).write_opening(_context())

    assert draft is None


async def test_the_null_opener_never_invents_a_scene() -> None:
    assert await NullStorySceneOpener().write_opening(_context()) is None
