"""Image-recognition summary placement + per-image ownership legend.

Regression for the 2026-07-15「留言有點難讀懂耶」incident (turn record
9b094fad): the recognition summary used to be appended AFTER the whole
assembled prompt — behind the instruction footer — so its analyst
register and OCR hedges ("模糊…無法可靠辨識") were the last tokens the
text-only chat model read, and it role-played them as "the user's
message is hard to read". These tests pin:

- the summary block renders inside the prompt body, adjacent to the
  ``圖片標記`` legend, BEFORE 近期對話 / 最新使用者訊息 / the
  instruction footer — never as the prompt tail;
- each ``[圖 N]`` gets an ownership line so the character can tell its
  own earlier image apart from what the user just sent;
- the legend no longer claims every inventoried image was "本回合附加"
  (history carry-over images belong to earlier turns);
- a guard line scopes any residual illegibility wording to the photo,
  not the user's message.
"""

from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character() -> Character:
    return Character.create(
        name="鈴音",
        summary="",
        personality=["活潑"],
        interests=[],
        speaking_style="輕快",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


_NOW = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)


def _mixed_history() -> list[Message]:
    """Assistant sent an image last turn ([圖 1]); the current user
    message carries [圖 2] — mirrors the incident's turn shape."""
    return [
        Message(
            role=MessageRole.ASSISTANT,
            content="木木～你家的貓最近還好嗎？",
            created_at=_NOW - timedelta(minutes=10),
        ),
    ]


def _build(
    *,
    recent_messages: list[Message],
    vision_markers: dict[int, list[int]],
    image_recognition_context: str = "",
    latest_user_message: str = "貓咪來了 給妳看金萱",
) -> str:
    character = _character()
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=recent_messages,
        memories=[],
        pending_state=character.state,
        latest_user_message=latest_user_message,
        now=_NOW,
        vision_markers=vision_markers,
        image_recognition_context=image_recognition_context,
    )


def test_summary_renders_in_body_not_as_prompt_tail() -> None:
    prompt = _build(
        recent_messages=_mixed_history(),
        vision_markers={0: [1], 1: [2]},
        image_recognition_context=(
            "[圖 1] 一名狐耳少女捧著飯糰。\n[圖 2] 一隻白橘色的貓仰躺在地磚上。"
        ),
    )

    summary_idx = prompt.index("圖片識別摘要")
    assert summary_idx < prompt.index("近期對話：")
    assert summary_idx < prompt.index("最新使用者訊息：")
    # The block must never be the final segment of the prompt — the
    # instruction footer stays last.
    assert not prompt.rstrip().endswith("[/圖片識別摘要]")
    assert prompt.index("[/圖片識別摘要]") < prompt.index("指示：")
    # Sits adjacent to (right after) the marker legend.
    assert prompt.index("圖片標記") < summary_idx


def test_summary_block_carries_photo_scope_guard() -> None:
    prompt = _build(
        recent_messages=_mixed_history(),
        vision_markers={0: [1], 1: [2]},
        image_recognition_context="[圖 1] 桌上有模糊的小字。",
    )
    # Guard line: illegible photo detail must not be read as the user's
    # message being hard to read.
    guard_idx = prompt.index("與對方訊息本身無關")
    assert prompt.index("[/圖片識別摘要]") < guard_idx < prompt.index("指示：")


def test_ownership_lines_distinguish_own_image_from_users() -> None:
    prompt = _build(
        recent_messages=_mixed_history(),
        vision_markers={0: [1], 1: [2]},
        image_recognition_context="[圖 1] 狐耳少女。\n[圖 2] 貓。",
    )

    assert "- [圖 1]：你自己稍早傳給對方的圖" in prompt
    assert "- [圖 2]：使用者這一輪剛傳來的圖" in prompt


def test_ownership_marks_users_earlier_image_as_history() -> None:
    history = [
        Message(
            role=MessageRole.USER,
            content="看看這張照片",
            created_at=_NOW - timedelta(minutes=5),
        ),
    ]
    prompt = _build(
        recent_messages=history,
        vision_markers={0: [1]},
        image_recognition_context="[圖 1] 一張街景。",
        latest_user_message="你覺得如何？",
    )

    assert "- [圖 1]：使用者稍早傳來的圖" in prompt


def test_legend_no_longer_claims_all_images_attached_this_turn() -> None:
    prompt = _build(
        recent_messages=_mixed_history(),
        vision_markers={0: [1], 1: [2]},
        image_recognition_context="[圖 1] 狐耳少女。\n[圖 2] 貓。",
    )
    assert "本回合附加了" not in prompt


def test_no_context_renders_no_summary_block() -> None:
    prompt = _build(
        recent_messages=_mixed_history(),
        vision_markers={0: [1], 1: [2]},
        image_recognition_context="",
    )
    assert "圖片識別摘要" not in prompt
    # Legend still renders for the markers themselves.
    assert "圖片標記" in prompt
