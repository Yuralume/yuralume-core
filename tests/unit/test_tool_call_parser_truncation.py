"""Tolerance tests for ``parse_tool_call`` — truncated / malformed JSON.

Regression: when the LLM's reply was cut off by max_tokens mid
tool-call, the raw ``{"tool": ...`` blob was leaking into the chat
bubble because the parser returned ``None`` and the chat loop fell
through to ``last_text``. These tests pin both the repair behaviour
(close the JSON and parse) and the "looks like a tool call" hint the
chat loop uses to suppress raw leaks.
"""

from __future__ import annotations

from kokoro_link.application.services.tool_call_parser import (
    looks_like_tool_call_attempt, parse_tool_call,
)


def test_parses_well_formed_tool_call_with_code_fence() -> None:
    raw = (
        '```json\n'
        '{"tool": "generate_image", "args": {"positive": "sunset"}}\n'
        '```'
    )
    call = parse_tool_call(raw)
    assert call is not None
    assert call.name == "generate_image"
    assert call.arguments == {"positive": "sunset"}


def test_repairs_truncated_tool_call_missing_outer_brace() -> None:
    """The exact shape the user hit: valid JSON up to inner args dict's
    closing ``}``, but the outer object's closing ``}`` never arrived
    because max_tokens chopped the response."""
    raw = (
        '```json\n'
        '{"tool": "generate_image", "args": {"positive": "1girl, solo", '
        '"aspect": "portrait", "caption": "走到窗邊"}'
    )
    call = parse_tool_call(raw)
    assert call is not None
    assert call.name == "generate_image"
    assert call.arguments["positive"] == "1girl, solo"
    assert call.arguments["aspect"] == "portrait"


def test_repairs_truncated_tool_call_cut_inside_string() -> None:
    """Cut happens mid-``caption`` string — we close the string and
    balance braces. The recovered caption is whatever tokens survived."""
    raw = (
        '{"tool": "generate_image", "args": {"positive": "short hair", '
        '"caption": "走到窗邊，月光灑在臉'
    )
    call = parse_tool_call(raw)
    assert call is not None
    assert call.name == "generate_image"
    assert call.arguments["positive"] == "short hair"
    # Caption got the truncated prefix, closed off for us.
    assert call.arguments["caption"].startswith("走到窗邊")


def test_does_not_rescue_random_non_tool_json() -> None:
    """Repair only runs when the text *looks* like a tool call. Random
    unbalanced JSON-ish text shouldn't magically become a tool call —
    that would mask genuine "model ignored tools, wrote prose with a
    stray brace" cases."""
    raw = "今天天氣很好{但我有點累"
    assert parse_tool_call(raw) is None


def test_looks_like_tool_call_detects_common_shapes() -> None:
    assert looks_like_tool_call_attempt('{"tool": "generate_image"')
    assert looks_like_tool_call_attempt(
        '```json\n{"tool": "echo", "args":'
    )
    # Whitespace and newlines between key and quote are fine.
    assert looks_like_tool_call_attempt('{\n  "tool"  :\n  "x",')
    # Prose that just mentions "tool" should NOT trigger.
    assert not looks_like_tool_call_attempt("I'll use a tool for this")
    assert not looks_like_tool_call_attempt("")


def test_returns_none_when_text_has_no_json_at_all() -> None:
    assert parse_tool_call("") is None
    assert parse_tool_call("  \n  ") is None
    assert parse_tool_call("just a normal chat reply") is None
