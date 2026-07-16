from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageContentMode,
    MessageRole,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.content_flow import CONTENT_TOLERANCE_COMMUNITY
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def test_frontier_prompt_omits_nsfw_marked_history() -> None:
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    prompt = DefaultPromptContextBuilder().build(
        character=character,
        conversation=conversation,
        recent_messages=[
            Message(role=MessageRole.USER, content="普通近況"),
            Message(
                role=MessageRole.ASSISTANT,
                content="NSFW 原文不可進 frontier",
                content_mode=MessageContentMode.NSFW,
            ),
        ],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
    )

    assert "普通近況" in prompt
    assert "NSFW 原文不可進 frontier" not in prompt


def test_frontier_prompt_uses_safe_summary_for_nsfw_marked_history() -> None:
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    prompt = DefaultPromptContextBuilder().build(
        character=character,
        conversation=conversation,
        recent_messages=[
            Message(
                role=MessageRole.ASSISTANT,
                content="NSFW 原文仍然不可進 frontier",
                content_mode=MessageContentMode.NSFW,
                safe_summary="角色與使用者延續親密但不露骨的互動情緒。",
            ),
        ],
        memories=[],
        pending_state=character.state,
        latest_user_message="繼續",
    )

    assert "角色與使用者延續親密但不露骨的互動情緒。" in prompt
    assert "NSFW 原文仍然不可進 frontier" not in prompt


def test_community_prompt_keeps_nsfw_marked_history() -> None:
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    prompt = DefaultPromptContextBuilder().build(
        character=character,
        conversation=conversation,
        recent_messages=[
            Message(
                role=MessageRole.USER,
                content="NSFW 原文可留在社群模型",
                content_mode=MessageContentMode.NSFW,
            ),
        ],
        memories=[],
        pending_state=character.state,
        latest_user_message="繼續",
        content_tolerance=CONTENT_TOLERANCE_COMMUNITY,
    )

    assert "NSFW 原文可留在社群模型" in prompt
