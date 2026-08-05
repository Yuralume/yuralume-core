"""Story scene wrap-up writer (SC1-D) — verdict, framing, and the red line.

Two jobs are under test. The mechanical one: an unfinished scene must
cost one cheap "no", a forced close must produce something usable, and
every failure mode must degrade rather than raise.

The load-bearing one is STORY_SCENE_PLAN §3.4 #1 — **a wrap-up may never
invent player actions**. The writer both instructs the model and verifies
it: any player action the wrap-up describes has to be quoted back from a
line the player really produced. These tests drive the verification with
scripted output, because the model's own compliance is not something a
test can assert; what it *can* assert is that a non-compliant draft never
reaches the thread or the character's memory.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.contracts.story_scene import StorySceneClosingContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_CLOSE_RESOLVED,
    SCENE_CLOSE_TIMEOUT,
    SCENE_LAYER_BEAT,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.story.llm_scene_closer import (
    LLMStorySceneCloser,
    NullStorySceneCloser,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 6, 1)


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


#: A scene the player walked out of: they said one thing and vanished.
#: Everything the red line is about lives in the gap after the last line.
ABANDONED_TRANSCRIPT = (
    "旁白：頂樓的風把譜架吹得直響。",
    "Aki：你怎麼還在這裡？",
    "玩家：我只是路過。",
    "Aki：……那你要不要留下來聽我拉完？",
)
ABANDONED_PLAYER_LINES = ("我只是路過。",)

#: A scene nobody ever answered in: the opening played and that was all.
SILENT_TRANSCRIPT = (
    "旁白：頂樓的風把譜架吹得直響。",
    "Aki：你怎麼還在這裡？",
)


def _context(
    *,
    mode: str = SCENE_CLOSE_TIMEOUT,
    transcript: tuple[str, ...] = ABANDONED_TRANSCRIPT,
    player_lines: tuple[str, ...] = ABANDONED_PLAYER_LINES,
    operator_position: str | None = None,
    operator_note: str | None = None,
) -> StorySceneClosingContext:
    return StorySceneClosingContext(
        character=_character(),
        session=StorySceneSession.open_scene(
            character_id="c1",
            conversation_id="conv-1",
            source_layer=SCENE_LAYER_BEAT,
            beat_id="beat-1",
            title="把話說完",
            location="頂樓天台",
            mood="欲言又止",
            scene_type="conflict",
            dramatic_question="她要承認自己練得不夠嗎？",
            operator_position=operator_position,
            operator_note=operator_note,
            opened_at=NOW,
        ),
        mode=mode,
        transcript=transcript,
        player_lines=player_lines,
        today=TODAY,
    )


# ── the red line: no invented player actions ─────────────────────────


async def test_a_wrap_up_that_claims_the_player_agreed_is_refused() -> None:
    """Negative case 1 — a promise the player never made.

    The player said "我只是路過" and left. A wrap-up in which they agreed
    to stay is the exact failure §3.4 #1 exists to prevent, and it would
    land in both the thread and the character's long-term memory.
    """
    model = _ScriptedModel(
        '{"resolved": true,'
        ' "closing_narration": "你答應留下來聽她拉完，她笑了。",'
        ' "canon_summary": "他答應留下來陪我練琴。",'
        ' "player_actions_referenced": ["我留下來聽你拉完"]}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(_context())

    assert draft is None


async def test_a_wrap_up_that_invents_a_physical_action_is_refused() -> None:
    """Negative case 2 — a gesture, cited from the *character's* line.

    Quoting something that was said in the scene is not enough: it has to
    have been said by the player. A citation lifted from Aki's own line
    is how a fabrication would otherwise slip through a naive check.
    """
    model = _ScriptedModel(
        '{"resolved": true,'
        ' "closing_narration": "你伸手按住她的肩，她沒有躲。",'
        ' "canon_summary": "他伸手按住我的肩膀。",'
        ' "player_actions_referenced": ["那你要不要留下來聽我拉完"]}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(_context())

    assert draft is None


async def test_any_citation_at_all_is_a_fabrication_when_nobody_spoke() -> None:
    """A scene the player never spoke in has no player actions to cite."""
    model = _ScriptedModel(
        '{"resolved": true,'
        ' "closing_narration": "你沉默地點了點頭。",'
        ' "canon_summary": "他點頭了。",'
        ' "player_actions_referenced": ["嗯"]}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(
        _context(transcript=SILENT_TRANSCRIPT, player_lines=()),
    )

    assert draft is None


async def test_a_wrap_up_about_the_character_alone_is_accepted() -> None:
    """The compliant shape: the camera stays on her, nothing is cited."""
    model = _ScriptedModel(
        '{"resolved": true,'
        ' "closing_narration": "你離開之後，她把譜架收起來，一個人又站了很久。",'
        ' "canon_summary": "他走了以後，我自己把那段又拉了一遍。",'
        ' "player_actions_referenced": []}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(_context())

    assert draft is not None
    assert draft.resolved is True
    assert "她把譜架收起來" in draft.closing_narration
    assert draft.canon_summary


async def test_a_citation_that_really_happened_is_accepted() -> None:
    """Quoting a real player line — even partially — is the sanctioned way
    to write about something the player actually did."""
    model = _ScriptedModel(
        '{"resolved": true,'
        ' "closing_narration": "你說你只是路過，然後就真的走了；她沒有追上去。",'
        ' "canon_summary": "他說只是路過就走了，我沒有留他。",'
        ' "player_actions_referenced": ["玩家：我只是路過"]}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(_context())

    assert draft is not None
    assert draft.canon_summary


# ── the prompt carries the rule too ──────────────────────────────────


async def test_the_prompt_states_the_red_line_and_the_scene_it_applies_to(
) -> None:
    model = _ScriptedModel('{"resolved": false}')

    await LLMStorySceneCloser(model=model).write_closing(
        _context(mode=SCENE_CLOSE_RESOLVED),
    )

    prompt = model.prompts[0]
    assert "不得描寫玩家做了任何逐字稿裡沒有出現的行動或回應" in prompt
    assert "player_actions_referenced" in prompt
    # the transcript is the evidence the rule is stated against
    assert "玩家：我只是路過。" in prompt
    assert "她要承認自己練得不夠嗎？" in prompt


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (SCENE_CLOSE_TIMEOUT, "你離開之後"),
        (SCENE_CLOSE_MANUAL, "你們就此打住"),
        (SCENE_CLOSE_RESOLVED, "由你判斷"),
    ],
    ids=["timeout", "manual", "verdict"],
)
async def test_each_mode_gets_its_own_framing(mode: str, expected: str) -> None:
    model = _ScriptedModel('{"resolved": false}')

    await LLMStorySceneCloser(model=model).write_closing(_context(mode=mode))

    assert expected in model.prompts[0]


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        ("central", "核心"),
        ("present", "在場，但不是核心"),
        ("absent", "原本不在場"),
    ],
    ids=["central", "present", "absent"],
)
async def test_each_known_position_gets_its_own_closing_framing(
    position: str, expected: str,
) -> None:
    model = _ScriptedModel('{"resolved": false}')

    await LLMStorySceneCloser(model=model).write_closing(
        _context(mode=SCENE_CLOSE_RESOLVED, operator_position=position),
    )

    assert expected in model.prompts[0]


async def test_an_unjudged_position_asks_the_closer_to_derive_one() -> None:
    model = _ScriptedModel('{"resolved": false}')

    await LLMStorySceneCloser(model=model).write_closing(
        _context(mode=SCENE_CLOSE_RESOLVED, operator_position=None),
    )

    assert "沒有人標記過" in model.prompts[0]


async def test_the_operator_note_reaches_the_closing_prompt() -> None:
    model = _ScriptedModel('{"resolved": false}')

    await LLMStorySceneCloser(model=model).write_closing(
        _context(
            mode=SCENE_CLOSE_RESOLVED,
            operator_position="central",
            operator_note="她要向你坦白",
        ),
    )

    assert "她要向你坦白" in model.prompts[0]


async def test_a_central_position_does_not_loosen_the_red_line() -> None:
    """Even the framing that puts the player at the centre of the scene
    must not license inventing what the player actually did or said."""
    model = _ScriptedModel(
        '{"resolved": true,'
        ' "closing_narration": "你答應留下來聽她拉完，她笑了。",'
        ' "canon_summary": "他答應留下來陪我練琴。",'
        ' "player_actions_referenced": ["我留下來聽你拉完"]}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(
        _context(operator_position="central"),
    )

    assert draft is None


async def test_a_scene_with_no_dialogue_says_so_rather_than_going_blank(
) -> None:
    model = _ScriptedModel('{"resolved": false}')

    await LLMStorySceneCloser(model=model).write_closing(
        _context(transcript=(), player_lines=()),
    )

    assert "這場戲只有開場" in model.prompts[0]


# ── verdict semantics ────────────────────────────────────────────────


async def test_an_unfinished_scene_answers_not_resolved_and_nothing_else(
) -> None:
    model = _ScriptedModel(
        '{"resolved": false, "closing_narration": "", "canon_summary": ""}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(
        _context(mode=SCENE_CLOSE_RESOLVED),
    )

    assert draft is not None
    assert draft.resolved is False


async def test_a_verdict_with_nothing_written_is_not_a_verdict() -> None:
    """``resolved`` with no prose would end a scene into silence; the
    scene keeps playing and the next turn asks again."""
    model = _ScriptedModel(
        '{"resolved": true, "closing_narration": "", "canon_summary": ""}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(
        _context(mode=SCENE_CLOSE_RESOLVED),
    )

    assert draft is not None and draft.resolved is False


async def test_a_forced_close_ignores_an_unfinished_verdict() -> None:
    """The player already pressed the button; the scene is over either way."""
    model = _ScriptedModel(
        '{"resolved": false,'
        ' "closing_narration": "你們就此打住，她把琴收了起來。",'
        ' "canon_summary": "我們今天聊到這裡。"}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(
        _context(mode=SCENE_CLOSE_MANUAL),
    )

    assert draft is not None and draft.resolved is True


async def test_a_forced_close_keeps_canon_even_without_narration() -> None:
    """Half an answer is still worth landing on a path that must close."""
    model = _ScriptedModel(
        '{"resolved": true, "closing_narration": "",'
        ' "canon_summary": "他今天沒說什麼就走了。"}',
    )

    draft = await LLMStorySceneCloser(model=model).write_closing(
        _context(mode=SCENE_CLOSE_MANUAL),
    )

    assert draft is not None
    assert draft.closing_narration == ""
    assert draft.canon_summary


# ── degradation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    ["not json at all", "{", '{"resolved": true}', "[]"],
    ids=["prose", "truncated", "no-prose", "not-an-object"],
)
async def test_unusable_output_is_no_draft_on_a_forced_close(
    reply: str,
) -> None:
    draft = await LLMStorySceneCloser(
        model=_ScriptedModel(reply),
    ).write_closing(_context(mode=SCENE_CLOSE_MANUAL))

    assert draft is None


async def test_an_upstream_failure_is_no_draft() -> None:
    draft = await LLMStorySceneCloser(
        model=_ScriptedModel("", raises=RuntimeError("upstream down")),
    ).write_closing(_context())

    assert draft is None


async def test_the_fake_backend_never_wraps_a_scene_up() -> None:
    """Deterministic junk would end a live scene and enter canon."""
    draft = await LLMStorySceneCloser(
        model=FakeChatModel("fake"),
    ).write_closing(_context(mode=SCENE_CLOSE_MANUAL))

    assert draft is None


async def test_the_null_closer_never_wraps_a_scene_up() -> None:
    assert await NullStorySceneCloser().write_closing(_context()) is None
