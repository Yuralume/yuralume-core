"""OP2-C — one beat, two writers, one framing of the player.

A beat's prose can be produced by the autonomous beat scene writer
(``story/beat_scene_writer``) or by the expander's scene mode
(``story/expander_scene``). Both end up as the same StoryEvent, so the
assertions here are mostly *comparisons between the two prompts*: it is
not enough that each one frames the player somehow, they have to frame
them the same way, or the beat reads like two different worlds depending
on which writer got to it.

The negative half is the SC1-D red line carried forward: neither writer
has a player transcript to check against — they write in one shot with
no player turn at all — so no position, not even ``central``, may
license inventing what the player said or did.
"""

from __future__ import annotations

from datetime import date, timezone

import pytest

from kokoro_link.application.services.story_event_service import (
    StoryEventService,
)
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.contracts.story import SceneContext, StoryEventExpanderPort
from kokoro_link.contracts.story_arc import StoryBeatSceneContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from kokoro_link.infrastructure.story.beat_operator_position import (
    render_beat_operator_position_block,
)
from kokoro_link.infrastructure.story.llm_beat_scene_writer import (
    LLMStoryBeatSceneWriter,
)
from kokoro_link.infrastructure.story.llm_expander import (
    LLMStoryEventExpander,
)


ALL_POSITIONS = [
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_PRESENT,
    OPERATOR_POSITION_CENTRAL,
    None,
]


class _CapturingModel:
    provider_id = "scripted"
    supports_vision = False

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs):  # noqa: ANN003, ARG002
        self.prompts.append(prompt)
        return '{"narrative": "我在門口停下。", "tone": null, "cast_strategy": "npc_dialogue"}'

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        yield await self.generate(prompt, **kwargs)

    async def list_models(self) -> list[str]:
        return ["scripted"]


class _DuckSeed:
    def __init__(self, seed_text: str) -> None:
        self.id = "arc-beat:test"
        self.seed_text = seed_text


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="想成為演員的學生。",
        personality=[],
        interests=[],
        speaking_style="坦率",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _beat(
    position: str | None,
    note: str | None = None,
    *,
    summary: str = "老師逼她承認自己想贏。",
) -> StoryArcBeat:
    return StoryArcBeat.create(
        arc_id="arc-1",
        sequence=0,
        scheduled_date=date(2026, 6, 1),
        title="最後提醒",
        summary=summary,
        tension=TENSION_RISING,
        scene_characters=("指導老師",),
        location="排練室門口",
        dramatic_question="她敢不敢承認自己想贏？",
        operator_position=position,
        operator_note=note,
    )


async def _autonomous_prompt(
    beat: StoryArcBeat, *, caller_directive: str = "",
) -> str:
    """Prompt the autonomous beat scene writer would send."""
    model = _CapturingModel()
    character = _character()
    arc = StoryArc.create(
        character_id=character.id,
        title="試鏡週",
        premise="她準備面對重要試鏡。",
        theme="ambition",
        tone="dramatic",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 1),
    )
    await LLMStoryBeatSceneWriter(model=model).write_scene(
        StoryBeatSceneContext(
            character=character,
            arc=arc.with_beats([beat]),
            beat=beat,
            today=date(2026, 6, 1),
            user_involvement_policy=caller_directive,
        ),
    )
    return model.prompts[0]


async def _expander_prompt(beat: StoryArcBeat) -> str:
    """Prompt the expander's scene mode would send for the same beat."""
    model = _CapturingModel()
    await LLMStoryEventExpander(model=model).expand(
        seed=_DuckSeed(beat.summary),
        character_name="Mio",
        character_summary="想成為演員的學生。",
        speaking_style="坦率",
        world_frame="modern",
        character=_character(),
        scene=SceneContext(
            scene_type="conflict",
            location=beat.location,
            scene_characters=beat.scene_characters,
            dramatic_question=beat.dramatic_question,
            tone="dramatic",
            today=date(2026, 6, 1),
            operator_position=beat.operator_position,
            operator_note=beat.operator_note,
        ),
    )
    return model.prompts[0]


# --- the two writers must agree ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ALL_POSITIONS)
async def test_both_writers_frame_the_player_with_the_same_words(
    position: str | None,
) -> None:
    """The whole ticket in one assertion.

    Not "both prompts mention the player" — byte-identical framing. Two
    hand-written vocabularies for the same enum is precisely how the
    same beat ends up reading like two different worlds.
    """
    beat = _beat(position)
    expected = render_beat_operator_position_block(position, None)

    assert expected in await _autonomous_prompt(beat)
    assert expected in await _expander_prompt(beat)


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ALL_POSITIONS)
async def test_neither_writer_may_invent_what_the_player_did(
    position: str | None,
) -> None:
    """SC1-D's red line, carried into writers that have no transcript.

    ``central`` is included on purpose: making the scene *about* the
    player widens where the camera points, never what may be put in
    their mouth. A branch that quietly dropped this line would be the
    easy mistake, so every branch is checked.
    """
    beat = _beat(position)
    autonomous = await _autonomous_prompt(beat)
    expander = await _expander_prompt(beat)

    for prompt in (autonomous, expander):
        assert "玩家說了什麼、做了什麼、答應了什麼，一律不得虛構" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ALL_POSITIONS)
async def test_the_note_reaches_both_writers(position: str | None) -> None:
    beat = _beat(position, "她要向你坦白瞞了三年的事。")

    for prompt in (
        await _autonomous_prompt(beat),
        await _expander_prompt(beat),
    ):
        assert "她要向你坦白瞞了三年的事。" in prompt
        assert "額外備註" in prompt


# --- each position says something different ---------------------------


@pytest.mark.asyncio
async def test_central_makes_the_scene_about_the_player() -> None:
    prompt = await _autonomous_prompt(_beat(OPERATOR_POSITION_CENTRAL))

    assert "玩家在這場戲的位置：核心" in prompt
    # …while still refusing to perform the player's half of it.
    assert "不要替玩家把這場戲演完" in prompt


@pytest.mark.asyncio
async def test_present_puts_the_player_in_the_room_without_the_spotlight() -> None:
    prompt = await _expander_prompt(_beat(OPERATOR_POSITION_PRESENT))

    assert "玩家在這場戲的位置：在場，但不是核心" in prompt
    assert "可以寫角色注意到玩家" in prompt


@pytest.mark.asyncio
async def test_absent_keeps_the_player_out_of_the_scene() -> None:
    prompt = await _autonomous_prompt(_beat(OPERATOR_POSITION_ABSENT))

    assert "玩家在這場戲的位置：不在場" in prompt
    assert "不要把玩家寫進場景" in prompt


@pytest.mark.asyncio
async def test_unjudged_hands_the_call_to_the_model() -> None:
    prompt = await _autonomous_prompt(_beat(None))

    assert "沒有人標記過" in prompt
    assert "請你自己從出場人物、戲劇問題與摘要判斷" in prompt


@pytest.mark.asyncio
async def test_an_unjudged_beat_is_framed_by_its_words_not_by_ours() -> None:
    """LLM-first (red line 1).

    A beat whose summary calls someone 「伴侶」 is the plan's own example
    of prose that *might* mean the player. The framing must be the same
    block either way — the verdict is the model's to reach from the
    material, and any difference here would mean a keyword table had
    crept into the code that renders it.
    """
    neutral = await _autonomous_prompt(
        _beat(None, summary="老師逼她承認自己想贏。"),
    )
    ambiguous = await _autonomous_prompt(
        _beat(None, summary="她想跟伴侶把瞞了三年的事說開。"),
    )

    block = render_beat_operator_position_block(None, None)
    assert block in neutral
    assert block in ambiguous


def test_an_unrecognised_position_degrades_to_unjudged() -> None:
    """A stale or hand-edited value must not take down a realization.

    The domain boundary already rejects these on write, so this is the
    read path being deliberately forgiving — same posture the storage
    decoders take (plan §3.1).
    """
    assert render_beat_operator_position_block(
        "bystander",
    ) == render_beat_operator_position_block(None)


# --- the caller override that replaced the default policy -------------


@pytest.mark.asyncio
async def test_a_caller_directive_is_rendered_below_the_red_line() -> None:
    """The simulate route can still steer one playthrough — but the
    override is framed as subordinate, so it can redirect the camera and
    not the red line."""
    prompt = await _autonomous_prompt(
        _beat(OPERATOR_POSITION_ABSENT),
        caller_directive="這次請讓老師主導整場對話。",
    )

    assert "這次請讓老師主導整場對話。" in prompt
    assert "不得放寬紅線" in prompt


@pytest.mark.asyncio
async def test_no_caller_directive_adds_nothing() -> None:
    prompt = await _autonomous_prompt(_beat(OPERATOR_POSITION_ABSENT))

    assert "呼叫端的額外指示" not in prompt


# --- the position must survive the trip into the expander -------------


def test_position_alone_is_enough_scene_structure() -> None:
    """A beat that says "this scene is about the player" but names no
    location, cast or question is exactly the beat whose framing must
    not be dropped — and before OP2-C it would have fallen through to
    the seed-style journal prompt, which has no player framing at all.
    """
    assert SceneContext(
        operator_position=OPERATOR_POSITION_CENTRAL,
    ).is_meaningful() is True
    assert SceneContext(operator_note="她要坦白").is_meaningful() is True
    # Unchanged: an entirely empty context still is not scene-shaped.
    assert SceneContext().is_meaningful() is False


class _RecordingExpander(StoryEventExpanderPort):
    def __init__(self) -> None:
        self.scenes: list[SceneContext | None] = []

    async def expand(self, *, scene=None, **kwargs):  # noqa: ANN003, ARG002
        self.scenes.append(scene)
        return ("她在門口停了三秒。", None)


@pytest.mark.asyncio
async def test_beat_realization_hands_both_slots_to_the_expander() -> None:
    """The seam that carries the two fields into the expander path.

    NOTE (reported as an OP2-C finding, not fixed here): this seam is
    currently *orphaned* — ``_build_and_persist_from_beat`` lost its
    only caller when ``ensure_today``'s arc branch switched to the
    attempt/recheck flow, so in production no beat reaches the expander
    today. The wiring is still asserted because the plan treats this as
    one of the two beat writers and OP2-E's acceptance assumes it too;
    if it is revived, it must already carry the player's position.
    """
    character = _character()
    event_repo = InMemoryStoryEventRepository()
    expander = _RecordingExpander()
    service = StoryEventService(
        gacha=StoryGachaService(
            seed_repository=InMemoryStorySeedRepository(),
            event_repository=event_repo,
        ),
        expander=expander,
        event_repository=event_repo,
        memory_repository=InMemoryMemoryRepository(),
        local_tz=timezone.utc,
    )
    beat = _beat(OPERATOR_POSITION_CENTRAL, "她要向你坦白。")

    event = await service._build_and_persist_from_beat(  # noqa: SLF001
        character, date(2026, 6, 1), beat,
    )

    assert event is not None
    assert expander.scenes
    scene = expander.scenes[0]
    assert scene is not None
    assert scene.operator_position == OPERATOR_POSITION_CENTRAL
    assert scene.operator_note == "她要向你坦白。"


@pytest.mark.asyncio
async def test_realized_beat_narrative_is_still_written_once() -> None:
    """Guardrail for the seam above: adding the two slots must not
    change what the realization path persists."""
    character = _character()
    event_repo = InMemoryStoryEventRepository()
    service = StoryEventService(
        gacha=StoryGachaService(
            seed_repository=InMemoryStorySeedRepository(),
            event_repository=event_repo,
        ),
        expander=_RecordingExpander(),
        event_repository=event_repo,
        memory_repository=InMemoryMemoryRepository(),
        local_tz=timezone.utc,
    )

    event = await service._build_and_persist_from_beat(  # noqa: SLF001
        character, date(2026, 6, 1), _beat(None),
    )

    assert event is not None
    assert event.narrative == "她在門口停了三秒。"
    stored = await event_repo.get_for_day(character.id, "2026-06-01")
    assert [e.id for e in stored] == [event.id]
