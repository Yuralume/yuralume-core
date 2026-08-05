"""Prompt builder — the 起幕 scene frame (SC1-C).

Three properties are pinned here, and only the first is about wording:

* a turn played inside a live scene carries the frame (place / mood /
  scene type / dramatic question) as a directive;
* it carries **exactly one** scene directive — the ordinary
  「今日場景指引」 block is suppressed, because both would be describing
  the same beat with opposite instructions;
* the moment the scene is gone the prompt is byte-identical to what a
  non-scene turn produced before SC1-C existed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.story_arc import (
    BEAT_PENDING,
    SCENE_CONFLICT,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_LAYER_BEAT,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import (
    DefaultPromptContextBuilder,
)

TODAY = date(2026, 6, 1)
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


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
    operator_position: str | None = None,
    operator_note: str | None = None,
) -> StoryArc:
    arc = StoryArc.create(
        character_id="c1",
        title="三週的試鏡",
        premise="她報名了一場從沒想過會報的試鏡。",
        theme="ambition",
        start_date=TODAY,
        end_date=date(2026, 6, 15),
    )
    beat = StoryArcBeat(
        id="b-today", arc_id=arc.id, sequence=0,
        scheduled_date=TODAY, title="第一次撞牆",
        summary="鏡子裡只剩自己，呼吸卻還是不夠穩。",
        tension=TENSION_RISING, status=BEAT_PENDING,
        scene_characters=("指導老師",), location="音樂教室",
        dramatic_question="她要承認自己練得不夠嗎？",
        scene_type=SCENE_CONFLICT, required=True,
        operator_position=operator_position,
        operator_note=operator_note,
    )
    return arc.with_beats([beat])


def _session(**overrides) -> StorySceneSession:  # noqa: ANN003
    fields = {
        "character_id": "c1",
        "conversation_id": "conv-1",
        "source_layer": SCENE_LAYER_BEAT,
        "title": "把話說完",
        "location": "頂樓天台",
        "mood": "欲言又止",
        "scene_type": SCENE_CONFLICT,
        "dramatic_question": "她要承認自己練得不夠嗎？",
        "opened_at": NOW,
    }
    fields.update(overrides)
    return StorySceneSession.open_scene(**fields)


def _build(
    *,
    scene: StorySceneSession | None = None,
    arc: StoryArc | None = None,
    conversation: Conversation | None = None,
    character: Character | None = None,
) -> str:
    builder = DefaultPromptContextBuilder()
    who = character or _character()
    return builder.build(
        character=who,
        conversation=conversation or Conversation.start(character_id=who.id),
        recent_messages=[],
        memories=[],
        pending_state=who.state,
        latest_user_message="我在。",
        story_arc=arc,
        story_scene=scene,
        today_local=TODAY,
    )


def test_scene_frame_is_rendered_while_the_scene_is_open() -> None:
    prompt = _build(scene=_session())

    assert "【劇情場景進行中】" in prompt
    assert "把話說完" in prompt
    assert "頂樓天台" in prompt
    assert "欲言又止" in prompt
    assert "衝突／拉扯" in prompt  # scene_type rendered as its label
    assert "她要承認自己練得不夠嗎？" in prompt


def test_scene_frame_suppresses_the_daily_beat_directive() -> None:
    """Two directives about the same beat pull in opposite directions.

    「今日場景指引」 tells the model to find a natural opening for a scene;
    the frame tells it the scene is already running. Handed both, models
    re-open a scene they are mid-way through — so exactly one renders.
    """
    prompt = _build(scene=_session(), arc=_arc_with_today_beat())

    assert "【劇情場景進行中】" in prompt
    assert "今日場景指引" not in prompt


def test_daily_beat_directive_survives_when_no_scene_is_open() -> None:
    prompt = _build(scene=None, arc=_arc_with_today_beat())

    assert "今日場景指引" in prompt
    assert "劇情場景進行中" not in prompt


def test_closed_scene_renders_nothing_and_restores_the_daily_directive() -> None:
    """After the curtain falls the turn is an ordinary turn again."""
    closed = _session().closed(reason=SCENE_CLOSE_MANUAL, at=NOW)

    prompt = _build(scene=closed, arc=_arc_with_today_beat())

    assert "劇情場景進行中" not in prompt
    assert "今日場景指引" in prompt


def test_non_scene_prompt_is_byte_identical_to_the_pre_sc1c_prompt() -> None:
    """The whole feature has to be invisible outside a scene.

    Same character, same conversation, same day: passing ``story_scene``
    explicitly as ``None`` must produce the very same bytes as a caller
    that has never heard of the argument.
    """
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    builder = DefaultPromptContextBuilder()

    def _legacy_call() -> str:
        return builder.build(
            character=character,
            conversation=conversation,
            recent_messages=[],
            memories=[],
            pending_state=character.state,
            latest_user_message="我在。",
            story_arc=_arc_with_today_beat(),
            today_local=TODAY,
        )

    assert _build(
        scene=None,
        arc=_arc_with_today_beat(),
        conversation=conversation,
        character=character,
    ) == _legacy_call()


def test_sparse_scene_frame_skips_the_labels_it_has_no_value_for() -> None:
    """A side story has no beat, so most frame fields are ``None``."""
    prompt = _build(
        scene=_session(
            title="",
            location=None,
            mood=None,
            scene_type=None,
            dramatic_question=None,
        ),
    )

    assert "【劇情場景進行中】" in prompt
    frame = prompt.split("【劇情場景進行中】")[1]
    assert "- 場景地點：" not in frame
    assert "- 氛圍：" not in frame
    assert "- 戲劇問題：" not in frame


def test_frame_forbids_narrating_the_player() -> None:
    """§3.4 red line 1's per-turn half: the model never plays the player."""
    prompt = _build(scene=_session())

    assert "不要替玩家決定他做了什麼" in prompt


@pytest.mark.parametrize("missing_scene", [None])
def test_builder_still_accepts_callers_that_never_heard_of_scenes(
    missing_scene: None,
) -> None:
    """Every non-chat caller (proactive, feed, …) omits ``story_scene``."""
    builder = DefaultPromptContextBuilder()
    character = _character()

    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="hi",
    )

    assert "劇情場景進行中" not in prompt


# --- OP2-A: player-position-derived framing (ARC_PLAYER_POSITION_PLAN) ---
#
# Characterization note: the retired sentence —
#   "你正在跟玩家演一段「劇情場景」——這不是普通聊天，是一場已經拉開序幕的
#   戲，玩家人就在戲裡。"
# — asserted the player's presence unconditionally, no matter what kind of
# beat the scene came from. The tests below pin its removal and the
# position-derived replacement.
#
# Two properties are pinned beyond "the right branch renders":
#
# * the framing comes off the **session** (OP2-D snapshot), never a
#   re-lookup of the beat — a beat edited/retired/realized mid-scene must
#   not downgrade a judged live scene to "unjudged";
# * the ``absent`` branch is the *已開幕* wording, not the *尚未開幕* one.
#   The opener already narrated the player walking into a scene that was
#   not originally about him; telling the next turn 「這場戲裡沒有玩家的
#   位置」/「不要假裝玩家在場」 contradicts the narration sitting two
#   messages above it. The shipped ``cafe_idol_audition`` template is
#   absent end to end, so this is every turn of it.


def test_scene_frame_retires_the_unconditional_player_presence_assertion() -> None:
    prompt = _build(scene=_session())
    assert "玩家人就在戲裡" not in prompt


def test_scene_frame_falls_back_to_unjudged_when_session_has_no_position() -> None:
    # A layer-3 side story has no beat to have judged a position, so the
    # session snapshot is ``None`` — the model is handed a derivation
    # instruction rather than a guess.
    prompt = _build(scene=_session(), arc=_arc_with_today_beat())
    assert "沒有人標記過" in prompt
    assert "他比較像是這場戲的核心、在場的陪伴" in prompt


def test_unjudged_live_scene_still_states_the_player_is_on_stage() -> None:
    """Unjudged is about *whose* story it is, never about whether the
    player is here — the opening narration already put him here."""
    prompt = _build(scene=_session())
    assert "玩家此刻人就在這場戲裡" in prompt


def test_scene_frame_derives_central_position_from_the_session() -> None:
    session = _session(operator_position="central")
    prompt = _build(scene=session)
    assert "玩家是這場戲的核心" in prompt
    assert "沒有人標記過" not in prompt


def test_scene_frame_derives_present_position_from_the_session() -> None:
    session = _session(operator_position="present")
    prompt = _build(scene=session)
    assert "戲的重心不在玩家身上" in prompt
    assert "他此刻在場" in prompt


def test_absent_live_scene_says_the_player_has_walked_in() -> None:
    session = _session(operator_position="absent")
    prompt = _build(scene=session)
    assert "這場戲原本不是關於玩家的" in prompt
    assert "玩家已經走進來了" in prompt


def test_absent_live_scene_never_tells_the_model_the_player_is_off_stage() -> None:
    """The High-2 regression, stated negatively.

    The 尚未開幕 wording for ``absent`` is 「這場戲裡沒有玩家的位置——這是你
    自己的戲，可以獨自演到底 (…) 不要假裝玩家在場見證整個過程」. Correct while
    the player is still chatting; a direct contradiction of the opening
    narration once the curtain is up.
    """
    session = _session(operator_position="absent")
    prompt = _build(scene=session, arc=_arc_with_today_beat(
        operator_position="absent",
    ))
    assert "這場戲裡沒有玩家的位置" not in prompt
    assert "可以獨自演到底" not in prompt
    assert "不要假裝玩家在場" not in prompt


def test_scene_frame_reads_the_session_not_the_live_beat() -> None:
    """A beat edited mid-performance must not re-frame the running scene.

    Same beat id, opposite verdicts: the session was opened while the beat
    said ``central``, the beat has since been edited to ``absent``. The
    turn keeps the frame the opener established.
    """
    arc = _arc_with_today_beat(operator_position="absent")
    session = _session(
        arc_id=arc.id, beat_id="b-today", operator_position="central",
    )
    prompt = _build(scene=session, arc=arc)
    assert "玩家是這場戲的核心" in prompt
    assert "這場戲原本不是關於玩家的" not in prompt


def test_scene_frame_survives_a_beat_that_can_no_longer_be_found() -> None:
    """Retired beat / mismatched arc / no arc resolved at all.

    The old best-effort lookup degraded to *unjudged* in each of these;
    the session snapshot means a judged scene stays judged.
    """
    judged = _session(
        arc_id="some-other-arc", beat_id="b-gone", operator_position="present",
    )
    for arc in (None, _arc_with_today_beat(operator_position="central")):
        prompt = _build(scene=judged, arc=arc)
        assert "戲的重心不在玩家身上" in prompt
        assert "沒有人標記過" not in prompt


def test_scene_frame_renders_operator_note_from_the_session() -> None:
    session = _session(
        operator_position="present", operator_note="她要向你坦白",
    )
    prompt = _build(scene=session)
    assert "- 玩家在這場戲的位置備註：她要向你坦白" in prompt


def test_pending_and_live_wordings_stay_distinct_for_the_same_beat() -> None:
    """The two situations must not converge back onto one sentence.

    Same ``absent`` beat, before and after 起幕: the daily directive tells
    the character to play it alone, the live frame tells it the player is
    already standing there.
    """
    arc = _arc_with_today_beat(operator_position="absent")

    pending = _build(scene=None, arc=arc)
    live = _build(scene=_session(operator_position="absent"), arc=arc)

    assert "這場戲裡沒有玩家的位置" in pending
    assert "玩家已經走進來了" not in pending
    assert "玩家已經走進來了" in live
    assert "這場戲裡沒有玩家的位置" not in live
