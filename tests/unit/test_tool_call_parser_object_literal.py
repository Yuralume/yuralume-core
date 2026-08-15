"""The two shape-only companions to ``looks_like_tool_call_attempt``.

The hint regex only knows our own ``{"tool": …}`` contract. Callers that
must decide "is this even a message for a human?" need a judgement that
survives the model reaching for a different key convention or quoting
style, so these look at shape alone.

They come in two widths on purpose:

* ``looks_like_object_literal`` — the whole answer is a structured
  literal. Safe on a turn that asked for prose.
* ``looks_like_tool_call_shape`` — the answer *contains* a call-shaped
  structure. Only for the turn that offered tools and demanded JSON
  alone.

Both files of tests below pin the boundary from the safe side too:
over-refusing costs a silently dropped promise and an endlessly retried
row, which is worse than the leak they exist to stop.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.tool_call_parser import (
    looks_like_object_literal,
    looks_like_tool_call_attempt,
    looks_like_tool_call_shape,
    parse_tool_call,
)


@pytest.mark.parametrize(
    "raw",
    [
        '{"tool": "web_search", "args": {"query": "夏祭"}}',
        '{"name": "generate_image", "arguments": {"positive": "自拍"}}',
        "{'tool': 'generate_image', 'args': {}}",
        '{"function_call": {"name": "web_search", "args": {}}}',
        '{"tool_name": "web_search", "parameters": {}}',
        '```json\n{"name": "web_search", "arguments": {}}\n```',
        '```\n{"tool": "web_search", "args": {}}\n```',
        '\n\n  {"tool": "web_search", "args": {}}  \n',
        "{}",
        # Upstreams that think in batches wrap the call in a list.
        '[{"name": "generate_image", "arguments": {"positive": "自拍"}}]',
        '[\n  {"tool": "web_search", "args": {"query": "夏祭"}}\n]',
    ],
)
def test_whole_answer_object_literals_are_recognised(raw: str) -> None:
    assert looks_like_object_literal(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   \n  ",
        "查到了！今年抽選是七月一號開始。",
        "Looked it up — the lottery opens on July 1st.",
        "查到囉～ 🎆 抽選 7/1 開始！",
        "{微笑}我查到了，抽選七月一號開始。",
        "抽選在 {7/1} 開始，我幫你標起來了。",
        '網站上寫著 {"date": "7/1"}，我猜是抽選開始日。',
        "我查完了。\n\n抽選是七月一號開始。",
        "{",
        "}",
        # A bracketed stage direction is prose, not a batched call: it
        # carries no object inside the brackets.
        "[微笑]我查到了，抽選七月一號開始。",
        "[微笑]",
        "抽選日期 [7/1] 我幫你圈起來了",
    ],
)
def test_prose_is_not_an_object_literal(raw: str) -> None:
    assert looks_like_object_literal(raw) is False


def test_it_catches_shapes_the_contract_hint_cannot() -> None:
    """The point of having both: the hint regex is contract-specific and
    misses every foreign convention, which is exactly the gap that let
    raw JSON reach players."""
    foreign = '{"name": "generate_image", "arguments": {}}'

    assert looks_like_tool_call_attempt(foreign) is False
    assert looks_like_object_literal(foreign) is True


# --- the wider, tool-pass-only width -----------------------------------


_CALL_SHAPES_WRAPPED_IN_SOMETHING_ELSE = [
    # A sentence in front of the call — the model narrating what it is
    # about to do, against the "output the JSON alone" instruction.
    '好的，我幫你查一下：\n{"name": "generate_image", "arguments": {"positive": "自拍"}}',
    # Markdown, because models format even when told not to.
    '## 工具呼叫\n\n{"name": "web_search", "arguments": {"query": "夏祭"}}',
    '這是我要呼叫的工具：\n```json\n{"name": "web_search", "arguments": {"query": "夏祭"}}\n```',
    # Batched-call habit, with prose around it.
    '我先查一下喔\n[{"name": "web_search", "arguments": {"query": "夏祭"}}]',
    # Trailing commentary after the blob.
    '{"name": "web_search", "arguments": {"query": "夏祭"}}\n查完再跟你說！',
    # A roleplay marker in front of the call: the scan must not stop at
    # the first unparseable brace region.
    '{微笑}好，我查一下：{"name": "web_search", "arguments": {"query": "夏祭"}} 等我一下',
]


@pytest.mark.parametrize("raw", _CALL_SHAPES_WRAPPED_IN_SOMETHING_ELSE)
def test_embedded_call_shapes_are_recognised_by_the_wide_width(raw: str) -> None:
    """These are the shapes that walked straight past every earlier
    guard: ``parse_tool_call`` refuses them (foreign key names), the
    whole-answer width refuses them (they don't open with ``{``), and
    the contract hint refuses them (no ``{"tool":``)."""
    assert parse_tool_call(raw) is None
    assert looks_like_object_literal(raw) is False
    assert looks_like_tool_call_attempt(raw) is False

    assert looks_like_tool_call_shape(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "查到了！今年抽選是七月一號開始。",
        "調べておいたよ。抽選は 7/1 からだって！",
        "Looked it up — the lottery opens on July 1st.",
        "查到囉～ 🎆 抽選 7/1 開始！",
        "{微笑}我查到了，抽選七月一號開始。",
        "抽選在 {7/1} 開始，我幫你標起來了。",
        "我查完了。\n\n抽選是七月一號開始。",
        # One flat level of quoted data is how a person quotes a page,
        # not how a model calls a tool.
        '網站上寫著 {"date": "7/1"}，我猜是抽選開始日。',
        '他們的 API 回 {"open": true}，看起來今天有開。',
        # A code snippet the character wrote out for the player.
        "你要的片段：\n```python\ndef greet(name):\n    return f\"哈囉 {name}\"\n```",
    ],
)
def test_the_wide_width_still_leaves_real_messages_alone(raw: str) -> None:
    """Over-refusing is the expensive failure: the promise is never sent
    and the row retries forever. The width stops at "nests like a call"
    for exactly that reason."""
    assert looks_like_tool_call_shape(raw) is False


def test_the_two_widths_are_ordered() -> None:
    """Whatever the narrow width refuses, the wide one refuses too —
    a caller picking the wide width never loses coverage."""
    narrow_only = "{'tool': 'generate_image', 'args': {}}"

    assert looks_like_object_literal(narrow_only) is True
    assert looks_like_tool_call_shape(narrow_only) is True
