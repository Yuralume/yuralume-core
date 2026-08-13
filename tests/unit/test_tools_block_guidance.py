"""Per-tool guidance in the tool-use prompt block.

Regression cover for a specific failure: the block's judgements were
written for ``generate_image`` and rendered unconditionally, so a model
offered ``web_search`` read "沒有具體畫面可給 → 別叫工具" and declined
to search — then sometimes narrated a search it never ran.
"""

from __future__ import annotations

from kokoro_link.contracts.prompt import PromptToolDescriptor
from kokoro_link.infrastructure.prompt.tools_block import render_tools_block


def _tool(name: str) -> PromptToolDescriptor:
    return PromptToolDescriptor(
        name=name,
        description=f"{name} 的用途說明",
        parameters_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )


def _render(*names: str, forced_tool_name: str | None = None) -> str:
    return "\n".join(
        render_tools_block(
            [_tool(name) for name in names], forced_tool_name=forced_tool_name,
        )
    )


def test_no_tools_renders_nothing() -> None:
    assert render_tools_block([]) == []


def test_image_guidance_is_absent_when_image_tool_is_not_offered() -> None:
    """The core regression: visual judgements must not reach a character
    that only has search, where they read as "don't call anything"."""
    block = _render("web_search")
    assert "具體畫面" not in block
    assert "生圖" not in block
    assert "自拍" not in block


def test_search_guidance_is_absent_when_search_tool_is_not_offered() -> None:
    block = _render("generate_image")
    assert "搜尋" not in block
    assert "我剛查了一下" not in block


def test_each_tool_gets_its_own_call_and_skip_triggers() -> None:
    block = _render("generate_image", "web_search")
    assert "該用 web_search 的時機：" in block
    assert "不必用 web_search 的時機：" in block
    assert "該用 generate_image 的時機：" in block
    assert "不必用 generate_image 的時機：" in block


def test_search_triggers_on_the_model_hedging() -> None:
    """"我記得好像" is the model telling itself it lacks a fact — the
    cheapest signal available that a lookup is warranted."""
    block = _render("web_search")
    assert "我記得好像" in block
    assert "查完再回答" in block


def test_search_skip_list_covers_companionship_not_difficulty() -> None:
    block = _render("web_search")
    assert "陪伴" in block
    assert "在你腦中，不在網路上" in block


def test_honesty_rule_is_shared_by_every_tool() -> None:
    """Applies whatever the character has: claiming an un-run tool is
    the failure that reaches the player as misinformation."""
    for names in (("web_search",), ("generate_image",), ("web_search", "generate_image")):
        block = _render(*names)
        assert "沒有實際輸出工具 JSON，就不准在回覆裡表現得好像用過工具" in block
        assert "也不要在文字裡假裝你使用過任何工具" in block


def test_fabricated_search_claim_is_called_out_by_example() -> None:
    block = _render("web_search")
    assert "我剛查了一下" in block
    assert "我看到新聞說" in block


def test_staying_in_character_is_not_a_reason_to_skip_search() -> None:
    block = _render("web_search")
    assert "查詢過程對使用者是隱形的" in block


def test_unknown_tools_still_render_identity_and_contract() -> None:
    """A tool without a guidance entry degrades to name + description +
    schema; the shared rules still apply to it."""
    block = _render("some_new_tool")
    assert "### some_new_tool" in block
    assert "some_new_tool 的用途說明" in block
    assert '{"tool": "工具名稱", "args": {...}}' in block
    assert "該用 some_new_tool 的時機" not in block


def test_call_contract_states_one_tool_per_turn() -> None:
    block = _render("web_search", "generate_image")
    assert "一回合只能呼叫一個工具" in block


def test_forced_tool_directive_overrides_the_judgement_framing() -> None:
    block = _render("generate_image", forced_tool_name="generate_image")
    assert "本回合強制工具呼叫" in block
    assert "**這回合必須呼叫 `generate_image` 工具**" in block
