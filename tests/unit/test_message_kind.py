"""Message kind filtering & persistence round-trip.

``MessageKind.TOOL_ONLY`` marks assistant turns whose only payload is
tool output (e.g. a bare ``/pic`` image URL). Those turns should be
excluded when we build dialogue context for downstream LLM passes
(schedule / arc / proactive summarisation), but still persist so the UI
can render the artefact.
"""

from __future__ import annotations

from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)


def _convo_with_mixed_kinds() -> Conversation:
    convo = Conversation.start(character_id="c-1")
    convo = convo.append(Message(role=MessageRole.USER, content="想看你的臉"))
    convo = convo.append(
        Message(role=MessageRole.ASSISTANT, content="", kind=MessageKind.TOOL_ONLY),
    )
    convo = convo.append(Message(role=MessageRole.USER, content="好可愛"))
    convo = convo.append(
        Message(role=MessageRole.ASSISTANT, content="謝謝你這樣說"),
    )
    return convo


def test_recent_messages_default_includes_tool_only() -> None:
    convo = _convo_with_mixed_kinds()
    recent = convo.recent_messages(10)
    assert len(recent) == 4
    assert any(m.kind is MessageKind.TOOL_ONLY for m in recent)


def test_recent_messages_exclude_tool_only_drops_bare_artefacts() -> None:
    convo = _convo_with_mixed_kinds()
    filtered = convo.recent_messages(10, exclude_tool_only=True)
    assert len(filtered) == 3
    assert all(m.kind is MessageKind.CHAT for m in filtered)
    assert [m.content for m in filtered] == ["想看你的臉", "好可愛", "謝謝你這樣說"]


def test_recent_messages_limit_applied_after_filter() -> None:
    convo = _convo_with_mixed_kinds()
    # With exclude_tool_only + limit=2 we want the last two *chat* turns,
    # not the last two raw messages (which would include the tool-only).
    filtered = convo.recent_messages(2, exclude_tool_only=True)
    assert [m.content for m in filtered] == ["好可愛", "謝謝你這樣說"]


def test_message_defaults_to_chat_kind() -> None:
    message = Message(role=MessageRole.USER, content="hi")
    assert message.kind is MessageKind.CHAT


def test_message_kind_values_are_strings() -> None:
    # Persistence layers store the enum's ``.value`` — keep the contract
    # explicit so a rename doesn't silently break the DB column.
    assert MessageKind.CHAT.value == "chat"
    assert MessageKind.TOOL_ONLY.value == "tool_only"
