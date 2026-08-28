"""Vision marker / image-recognition prompt renderers."""

from typing import Mapping

from kokoro_link.domain.entities.conversation import Message
from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)

def _format_marker_prefix(numbers: list[int]) -> str:
    """``[1, 2]`` → ``"[圖 1][圖 2] "`` (trailing space included so
    callers can splice straight in front of the message text)."""
    if not numbers:
        return ""
    return "".join(f"[圖 {n}]" for n in numbers) + " "


def _render_vision_ownership_lines(
    markers: "Mapping[int, list[int]]",
    messages: list[Message],
) -> list[str]:
    """One line per ``[圖 N]`` naming who sent it.

    The vision inventory carries history images across turns, so the
    character must be able to tell its own earlier send (whose content
    it already "knows") apart from what the user just attached —
    otherwise it reacts to its own image as if the user sent it.
    Index ``len(messages)`` is the current user turn's slot.
    """
    current_turn = len(messages)
    entries: list[tuple[int, str]] = []
    for turn_idx, numbers in markers.items():
        if turn_idx == current_turn:
            source = "使用者這一輪剛傳來的圖"
        elif (
            0 <= turn_idx < len(messages)
            and messages[turn_idx].role.value == "assistant"
        ):
            source = "你自己稍早傳給對方的圖（內容你本來就知道）"
        else:
            source = "使用者稍早傳來的圖"
        entries.extend((number, source) for number in numbers)
    entries.sort()
    return [f"- [圖 {number}]：{source}" for number, source in entries]


def _render_image_recognition_block(context: str) -> list[str]:
    """Wrap the multimodal recognition summary for a text-only main model.

    Rendered in the prompt body next to the ``圖片標記`` legend (see the
    call site) — the closing guard line scopes any illegible-photo-text
    wording to the photo itself so the model doesn't tease the user
    about an "unreadable message".
    """
    cleaned = (context or "").strip()
    if not cleaned:
        return []
    return [
        "[圖片識別摘要：以下由系統的多模態模型產生，依 [圖 N] 順序"
        "描述上述圖片的畫面內容，供目前純文字模型理解圖片；"
        "這是系統提供的背景資料，不是使用者傳的文字。]",
        cleaned,
        "[/圖片識別摘要]",
        "（摘要若略過或看不清圖中某些小字，那只是照片細節的限制，"
        "與對方訊息本身無關；不要因此評論對方訊息難懂或難讀。）",
    ]


# --------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------

def _vision_legend(ctx: PromptSectionContext) -> list[str]:
    """Tell the model what the ``[圖 N]`` markers in history mean.

    Only emitted when at least one marker exists so a vision-less turn
    doesn't get a useless explainer. The inventory carries history images
    too (including the character's own earlier sends), so the legend maps
    every marker to its sender instead of claiming everything was attached
    this turn.
    """
    markers = ctx.vision.markers
    if not markers:
        return []
    total = sum(len(v) for v in markers.values())
    return [
        f"圖片標記：下方對話中共有 {total} 張圖片附件，"
        f"以 [圖 1]、[圖 2] … 依序標記；[圖 N] 出現在哪一則訊息，"
        "就代表那張圖是隨那則訊息附上的，"
        "可自然地參照「剛才那張」「上一張裡的那個」等指涉。",
        *_render_vision_ownership_lines(
            markers, list(ctx.dialogue.recent_messages),
        ),
    ]


def _image_recognition(ctx: PromptSectionContext) -> list[str]:
    """Recognition summary (text-only main model) renders HERE — in the
    body, adjacent to the legend — never appended after the instruction
    footer. Tail placement made its analyst register and OCR hedges the
    last tokens before generation, and the model role-played them as "the
    user's message is hard to read" (turn record 9b094fad, 2026-07-15)."""
    return _render_image_recognition_block(ctx.vision.image_recognition_context)


SECTIONS: tuple[PromptSection, ...] = (
    section("vision_legend", _vision_legend),
    section("image_recognition", _image_recognition),
)
