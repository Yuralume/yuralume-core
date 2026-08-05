"""Prompt builder — today's scene directive block rendering.

Phase 1 of ``docs/SCENE_BEAT_PLAN.md`` introduces a separate
"今日場景指引" segment that surfaces today's beat as a directive
(location / NPCs / dramatic question) rather than just paragraph
narrative. Tested in isolation here so a future tweak to the main
``build()`` ordering doesn't quietly drop the directive.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.story_arc import (
    ARC_ACTIVE,
    BEAT_PENDING,
    BEAT_REALIZED,
    SCENE_CONFLICT,
    SCENE_REVELATION,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import (
    DefaultPromptContextBuilder,
)

UTC = timezone.utc


def _character() -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _arc_with_today_beat(
    *,
    today: date,
    location: str | None = "音樂教室",
    npcs: tuple[str, ...] = ("指導老師",),
    question: str | None = "她要承認自己練得不夠嗎？",
    scene_type: str = SCENE_CONFLICT,
    required: bool = True,
    status: str = BEAT_PENDING,
    operator_position: str | None = None,
    operator_note: str | None = None,
) -> StoryArc:
    arc = StoryArc.create(
        character_id="c1",
        title="三週的試鏡",
        premise="她報名了一場從沒想過會報的試鏡。",
        theme="ambition",
        start_date=today,
        end_date=date(today.year, today.month, today.day + 14)
        if today.day + 14 <= 28 else date(today.year, today.month + 1, 5),
    )
    beat = StoryArcBeat(
        id="b-today", arc_id=arc.id, sequence=0,
        scheduled_date=today, title="第一次撞牆",
        summary="鏡子裡只剩自己，呼吸卻還是不夠穩。" * 3,
        tension=TENSION_RISING, status=status,
        scene_characters=npcs, location=location,
        dramatic_question=question, scene_type=scene_type,
        required=required,
        operator_position=operator_position,
        operator_note=operator_note,
    )
    return arc.with_beats([beat])


def _build(arc: StoryArc | None, today: date | None) -> str:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="hi",
        story_arc=arc,
        today_local=today,
    )


def test_directive_rendered_when_today_beat_has_scene_structure() -> None:
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(today=today)
    prompt = _build(arc, today)
    # Strong header signals a directive block, not informational.
    assert "【今日場景指引（必演）】" in prompt
    assert "音樂教室" in prompt
    assert "指導老師" in prompt
    assert "她要承認自己練得不夠嗎？" in prompt
    # The realized-beat title gives the LLM a one-phrase anchor.
    assert "第一次撞牆" in prompt


def test_directive_uses_optional_header_when_required_false() -> None:
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(today=today, required=False)
    prompt = _build(arc, today)
    assert "可選" in prompt
    assert "必演" not in prompt.split("【今日場景指引")[1].split("】")[0]


def test_directive_skipped_for_legacy_beat_without_scene_structure() -> None:
    # Older arcs persisted before Phase 1 had no location / NPCs /
    # question. The directive block must be silent for them so we
    # don't dump an empty header into the prompt.
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(
        today=today, location=None, npcs=(), question=None,
    )
    prompt = _build(arc, today)
    assert "今日場景指引" not in prompt


def test_directive_skipped_when_beat_already_realized() -> None:
    # Direction B keeps due beats pending until the scene actually
    # happens. Once realized, the beat should flow through StoryEvent /
    # memory and must not be forced into the prompt again.
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(today=today, status=BEAT_REALIZED)
    prompt = _build(arc, today)
    assert "今日場景指引" not in prompt


def test_directive_skipped_when_no_arc() -> None:
    today = date(2026, 5, 1)
    prompt = _build(None, today)
    assert "今日場景指引" not in prompt


def test_directive_skipped_when_no_today_local() -> None:
    # Prompt path that doesn't supply ``today_local`` (some callers in
    # tests / proactive paths) shouldn't crash and shouldn't render
    # the directive (we'd have no anchor day).
    arc = _arc_with_today_beat(today=date(2026, 5, 1))
    prompt = _build(arc, None)
    assert "今日場景指引" not in prompt


def test_directive_picks_revelation_label() -> None:
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(
        today=today,
        scene_type=SCENE_REVELATION,
        location="校園長椅",
        npcs=(),
        question="她真正想要的是什麼？",
    )
    prompt = _build(arc, today)
    assert "頓悟／揭露" in prompt
    assert "校園長椅" in prompt
    # Single-actor scene → no NPC line.
    assert "出場人物（除你之外）" not in prompt


# --- OP2-A: player-position-derived framing (ARC_PLAYER_POSITION_PLAN) ---
#
# Characterization note: the retired instruction —
#   "在合適時機讓這場戲自然發生 —— 場景的氣氛、出場人物的存在、戲劇問題的
#   張力，至少要在本回合的回覆中**浮現一個**。如果使用者岔題，你可以自然
#   地把話題引回來。"
# — used to render unconditionally whenever the directive block rendered
# at all, regardless of the beat's content. It gave the player exactly one
# identity in this block: someone who might digress. The tests below pin
# its removal and the position-derived replacement that took its place.


def test_directive_retires_the_forced_single_element_instruction() -> None:
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(today=today)  # operator_position unjudged
    prompt = _build(arc, today)
    assert "至少要在本回合的回覆中" not in prompt
    assert "浮現一個" not in prompt
    assert "如果使用者岔題，你可以自然地把話題引回來" not in prompt


def test_directive_none_position_uses_semantic_derivation_instruction() -> None:
    # Default/unjudged (`operator_position=None`) must not invent a
    # position — it hands the model a judgment instruction instead.
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(today=today, operator_position=None)
    prompt = _build(arc, today)
    assert "位置尚未判定" in prompt
    assert "由你自行判斷是否即指玩家" in prompt


def test_directive_central_position_frames_player_as_the_point_of_the_scene() -> None:
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(today=today, operator_position="central")
    prompt = _build(arc, today)
    assert "玩家是這場戲的核心" in prompt
    assert "位置尚未判定" not in prompt


def test_directive_present_position_frames_player_as_accompanying() -> None:
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(today=today, operator_position="present")
    prompt = _build(arc, today)
    assert "戲的重心不在玩家身上" in prompt


def test_directive_absent_position_frames_scene_without_the_player() -> None:
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(today=today, operator_position="absent")
    prompt = _build(arc, today)
    assert "這場戲裡沒有玩家的位置" in prompt


def test_directive_renders_operator_note_regardless_of_position() -> None:
    today = date(2026, 5, 1)
    arc = _arc_with_today_beat(
        today=today, operator_position="present", operator_note="她要向你坦白",
    )
    prompt = _build(arc, today)
    assert "- 玩家在這場戲的位置備註：她要向你坦白" in prompt
