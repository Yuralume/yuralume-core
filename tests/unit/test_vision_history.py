"""Tests for cross-turn image carry-over.

``ChatService._build_vision_inventory`` decides which image
attachments get forwarded to the model for a given turn:

- Newest-first FIFO, capped at ``_VISION_HISTORY_LIMIT`` (default 2)
- Chronological marker numbering so ``image_urls[i]`` lines up with
  the ``[圖 i+1]`` placeholder the prompt builder splices into the
  matching turn's history line.

These tests pin the contract; a regression here silently mis-pairs
images with turns, which degrades quietly (model reasons about the
wrong image).
"""

from __future__ import annotations

from kokoro_link.application.services.chat_service import (
    _VISION_HISTORY_LIMIT,
    _build_vision_inventory,
)
from kokoro_link.domain.entities.conversation import (
    Message, MessageAttachment, MessageRole,
)


def _msg(role: MessageRole, content: str, urls: list[str]) -> Message:
    return Message(
        role=role,
        content=content,
        attachments=tuple(
            MessageAttachment(kind="image", url=u, mime_type="image/*")
            for u in urls
        ),
    )


def test_returns_empty_when_no_images_anywhere() -> None:
    history = [
        _msg(MessageRole.USER, "嗨", []),
        _msg(MessageRole.ASSISTANT, "你好", []),
    ]
    urls, markers = _build_vision_inventory(
        recent_messages=history, current_user_urls=(),
    )
    assert urls == []
    assert markers == {}


def test_cap_drops_oldest_first() -> None:
    """Three history images + one current → over cap 2, drop the oldest
    two, keep the newest two. Markers renumber chronologically."""
    history = [
        _msg(MessageRole.USER, "看這張", ["/uploads/a.png"]),
        _msg(MessageRole.USER, "再看這張", ["/uploads/b.png"]),
        _msg(MessageRole.USER, "還有這張", ["/uploads/c.png"]),
    ]
    urls, markers = _build_vision_inventory(
        recent_messages=history,
        current_user_urls=("/uploads/d.png",),
        cap=2,
    )
    # Kept: newest 2 in chronological order (c, d).
    assert urls == ["/uploads/c.png", "/uploads/d.png"]
    # c belongs to history index 2, d belongs to the current turn
    # (synthetic index == len(history) == 3).
    assert markers == {2: [1], 3: [2]}


def test_marker_numbers_follow_appearance_order() -> None:
    """Two images in one history message + one in the current turn.
    Markers 1, 2 for the pair, 3 for current."""
    history = [
        _msg(MessageRole.USER, "兩張", ["/uploads/a.png", "/uploads/b.png"]),
    ]
    urls, markers = _build_vision_inventory(
        recent_messages=history,
        current_user_urls=("/uploads/c.png",),
        cap=5,
    )
    assert urls == ["/uploads/a.png", "/uploads/b.png", "/uploads/c.png"]
    assert markers == {0: [1, 2], 1: [3]}


def test_cap_zero_disables_vision_history() -> None:
    """Knob to turn it off without changing call sites — same contract
    as ``_VISION_HISTORY_LIMIT=0``."""
    history = [
        _msg(MessageRole.USER, "看", ["/uploads/a.png"]),
    ]
    urls, markers = _build_vision_inventory(
        recent_messages=history,
        current_user_urls=("/uploads/b.png",),
        cap=0,
    )
    assert urls == []
    assert markers == {}


def test_default_cap_matches_module_constant() -> None:
    """If the default cap changes we want the test to fail loudly —
    this is the knob operators tune, not a private detail."""
    assert _VISION_HISTORY_LIMIT == 2


def test_only_image_attachments_counted() -> None:
    """A non-image attachment (future-proofing — e.g. audio/file)
    should be ignored by the vision inventory."""
    other_att = MessageAttachment(
        kind="audio", url="/uploads/song.mp3", mime_type="audio/mpeg",
    )
    msg = Message(
        role=MessageRole.USER, content="聽", attachments=(other_att,),
    )
    urls, markers = _build_vision_inventory(
        recent_messages=[msg], current_user_urls=(),
    )
    assert urls == []
    assert markers == {}


def test_current_user_urls_land_at_the_end() -> None:
    """Cap=1 → only the current user image survives, historical ones drop.

    Matches the "newest wins" policy: the user's latest upload should
    always be visible when the budget is tight."""
    history = [
        _msg(MessageRole.USER, "舊圖", ["/uploads/old.png"]),
    ]
    urls, markers = _build_vision_inventory(
        recent_messages=history,
        current_user_urls=("/uploads/new.png",),
        cap=1,
    )
    assert urls == ["/uploads/new.png"]
    # Marker attaches to the synthetic "current turn" slot.
    assert markers == {1: [1]}
