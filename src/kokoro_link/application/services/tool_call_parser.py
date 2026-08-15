"""Parse a chat model's raw reply into an optional ``ToolCall``.

Tolerant of:

- leading / trailing whitespace
- ``\u0060\u0060\u0060json ... \u0060\u0060\u0060`` code fences
- short natural-language preamble the model sometimes emits even when
  we told it not to
- **truncated output** — when the model's response was cut short mid
  tool-call (max_tokens hit, stream dropped, model decided to stop),
  the JSON can end without closing braces. Rather than bailing out and
  leaking a half-formed ``{"tool": ...`` blob to the user, we
  auto-close open strings + braces and retry ``json.loads``. Covers
  the most common truncation shapes without becoming a full JSON5
  parser.

Also exposes ``looks_like_tool_call_attempt`` so the chat loop can
recognise "model tried to call a tool but we couldn't parse" and
swap the raw JSON for a friendlier fallback rather than dumping it
into the chat bubble. That helper keys off our own ``{"tool": …}``
contract, so it only recognises *our* shape.

Two shape-only companions serve callers that must decide "is this a
message for a human at all?" without assuming the model honoured the
contract's key names or quoting style. They differ only in width, and
the width is the caller's choice, not a detail to blur together:

- ``looks_like_object_literal`` — the *whole* answer is a structured
  literal. Safe on any turn, including ones where the model was asked
  for prose and nothing else.
- ``looks_like_tool_call_shape`` — the answer *contains* something
  shaped like a call (name plus a bag of arguments), even wrapped in a
  sentence or a markdown heading. Only appropriate on a turn that
  actually offered tools and told the model to emit JSON alone; on any
  other turn the same width would start eating messages that merely
  quote data.

Returns ``None`` whenever the reply clearly isn't structured as a tool
call — the caller should then treat the reply as the user-facing
answer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Final

from kokoro_link.domain.value_objects.tool_call import ToolCall


_TOOL_CALL_HINT_RE = re.compile(r'\{\s*"tool"\s*:\s*"', re.DOTALL)

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*|\s*```$")

_CLOSERS: Final[dict[str, str]] = {"{": "}", "[": "]"}

_MAX_SHAPE_SCAN_CHARS: Final = 8000
"""Ceiling on the text we scan for embedded structures. A composer reply
is capped at 800 chars long before it gets here; the bound only exists
so a pathological blob of unbalanced braces can't turn the scan into a
quadratic walk."""


def parse_tool_call(raw: str) -> ToolCall | None:
    if not raw or not raw.strip():
        return None
    obj = _extract_first_object(raw)
    if obj is None:
        obj = _repair_truncated_object(raw)
    if obj is None:
        return None
    name = obj.get("tool")
    if not isinstance(name, str) or not name.strip():
        return None
    args_raw = obj.get("args", {})
    if not isinstance(args_raw, dict):
        return None
    try:
        return ToolCall(name=name.strip(), arguments=args_raw)
    except ValueError:
        return None


def looks_like_tool_call_attempt(raw: str) -> bool:
    """Return ``True`` when the model's output *looks* like a tool call even
    if it doesn't parse.

    Used by the chat loop to decide whether to hide a malformed JSON blob
    from the user (replacing it with a fallback reply or retrying) rather
    than shipping raw ``{"tool": ...`` text into the chat bubble.
    """
    if not raw:
        return False
    return _TOOL_CALL_HINT_RE.search(raw) is not None


def looks_like_object_literal(raw: str) -> bool:
    """Return ``True`` when the model's *whole* answer is a structured
    literal rather than something written for a human.

    Deliberately shape-only: fences stripped, then "the entire answer
    opens with ``{`` and closes with ``}``" (or is a bracketed list that
    contains an object — the batched-call shape upstreams reach for when
    they think in arrays). It says nothing about key names or quoting,
    so it still catches the shapes our contract never asked for —
    ``{"name": …, "arguments": …}`` (OpenAI function-call habit),
    ``{'tool': …}`` (single quotes, so ``json.loads`` fails),
    ``{"tool_name": …}`` — and it will keep catching whatever the next
    upstream model's habit turns out to be. Enumerating the key names we
    have seen so far would only ever be a list of yesterday's shapes.

    Callers use this to refuse to *ship* such an answer to a player, not
    to interpret it: an unparseable literal is "no output" (retry next
    tick), never "the message".

    This is the width that is safe on **every** turn, including turns
    that offered no tools at all: prose is left alone. A reply that
    merely *contains* braces (``{微笑}你來啦``, ``抽選在 {7/1} 開始``)
    does not both open and close with them, a bracketed stage direction
    (``[微笑]``) carries no object inside it, and a character message
    that is entirely one brace pair is not a message worth sending
    either way.
    """
    if not raw:
        return False
    text = _FENCE_RE.sub("", raw.strip()).strip()
    if len(text) < 2:
        return False
    if text.startswith("{") and text.endswith("}"):
        return True
    return text.startswith("[") and text.endswith("]") and "{" in text


def looks_like_tool_call_shape(raw: str) -> bool:
    """Return ``True`` when the answer *contains* a failed tool call.

    The wider companion to :func:`looks_like_object_literal`, for the
    one turn where the extra width is earned: the prompt offered tools
    and told the model to emit the call JSON **alone** this turn. On
    that turn a structure shaped like a call, sitting inside a sentence
    ("好的，我幫你查一下：{…}") or under a markdown heading, is a call
    the model failed to emit cleanly — not a message it wrote for the
    player. Shipping it puts machine output in the chat bubble.

    The judgement stays structural. A tool call is, by construction,
    *a name plus a bag of arguments* — two levels of container — while
    prose that happens to quote data (``網站上寫著 {"date": "7/1"}``) is
    one flat level. So we look for an embedded JSON structure that
    nests: an object holding an object (or a list of them), or a list of
    objects. No key names, no keyword list — the same reason
    ``looks_like_object_literal`` refuses to enumerate conventions.

    Never use this width on a turn that offered no tools: there the
    model was asked for prose, and "contains a nested structure" would
    start eating legitimate messages for no matching gain.
    """
    if not raw:
        return False
    if looks_like_object_literal(raw):
        return True
    text = _FENCE_RE.sub("", raw.strip())[:_MAX_SHAPE_SCAN_CHARS]
    return any(_is_call_shaped(value) for value in _iter_embedded_json(text))


def _is_call_shaped(value: Any) -> bool:
    """Does this parsed structure carry a call's two-level shape?"""
    if isinstance(value, list):
        return bool(value) and all(isinstance(item, dict) for item in value)
    if not isinstance(value, dict):
        return False
    return any(_is_container(item) for item in value.values())


def _is_container(value: Any) -> bool:
    if isinstance(value, dict):
        return True
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) for item in value
    )


def _iter_embedded_json(text: str) -> Iterator[Any]:
    """Yield every top-level ``{…}`` / ``[…]`` region of ``text`` that
    parses as JSON, in order, skipping the ones that don't.

    Skipping rather than bailing is what keeps roleplay markers from
    hiding a real call behind them: ``{微笑}好，我查一下：{"name": …}``
    has to reach the second region.
    """
    index = 0
    length = len(text)
    while index < length:
        if text[index] not in _CLOSERS:
            index += 1
            continue
        end = _balanced_end(text, index)
        if end is None:
            index += 1
            continue
        try:
            yield json.loads(text[index : end + 1])
        except json.JSONDecodeError:
            pass
        index = end + 1


def _balanced_end(text: str, start: int) -> int | None:
    """Index of the bracket closing the one at ``start``, or ``None``
    when the region never closes (truncated output, stray brace)."""
    stack: list[str] = []
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSERS:
            stack.append(_CLOSERS[char])
        elif char in ("}", "]"):
            if not stack or stack[-1] != char:
                return None
            stack.pop()
            if not stack:
                return index
    return None


def _extract_first_object(text: str) -> dict[str, Any] | None:
    """Find the first top-level ``{...}`` JSON object in ``text``.

    Shares the idea (but not the code) with ``post_turn.llm_processor``
    so tweaks there don't inadvertently change chat tool-call parsing.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _repair_truncated_object(text: str) -> dict[str, Any] | None:
    """Best-effort recovery for a truncated ``{...}`` tool-call blob.

    Scenario: the model started emitting a valid JSON tool call but the
    response cut off before the final ``}`` (or mid-string). Without
    this, ``_extract_first_object`` returns ``None`` and the loop
    leaks the raw JSON to the user. We append just enough closing
    characters to rebalance the structure, then retry ``json.loads``.

    Only runs when the text *looks* like a tool call — we don't want to
    silently rescue random curly-brace soup into dicts.
    """
    if not looks_like_tool_call_attempt(text):
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    last_key_comma = -1  # position AFTER last safe comma at depth==1 inside outer obj
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    if depth <= 0 and not in_string:
        return None  # structurally fine — real parse would have worked
    suffix = ""
    if in_string:
        # Close the dangling string. If the last char before the cutoff
        # was a backslash we also need to drop the escape so the closer
        # isn't itself escaped.
        if text.endswith("\\"):
            suffix += "\\"
        suffix += '"'
    # After closing the string, assume the cut happened mid-value — drop
    # any trailing comma we couldn't have reached yet.
    candidate = text[start:] + suffix
    # Strip trailing comma at the end (e.g. ``"foo": "bar",`` with value
    # never arriving) — json.loads rejects those.
    candidate = candidate.rstrip()
    if candidate.endswith(","):
        candidate = candidate[:-1]
    candidate += "}" * depth
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        _ = last_key_comma  # silence unused — reserved for future trims
        return None
    return parsed if isinstance(parsed, dict) else None
