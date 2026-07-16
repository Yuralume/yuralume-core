from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.infrastructure.self_repetition.llm_extractor import _build_prompt


def test_long_turn_snippet_includes_head_and_tail() -> None:
    long_middle = "中段內容" * 80
    message = Message(
        role=MessageRole.ASSISTANT,
        content=f"開頭語氣很固定。{long_middle}最後又說，要不要我幫你整理一下？",
    )

    prompt = _build_prompt(
        character_name="Airi",
        assistant_messages=[message],
    )

    assert "開頭語氣很固定" in prompt
    assert "要不要我幫你整理一下？" in prompt


def test_short_turn_snippet_is_not_duplicated() -> None:
    message = Message(role=MessageRole.ASSISTANT, content="短短一句就好。")

    prompt = _build_prompt(
        character_name="Airi",
        assistant_messages=[message],
    )

    assert prompt.count("短短一句就好。") == 1
