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
into the chat bubble.

Returns ``None`` whenever the reply clearly isn't structured as a tool
call — the caller should then treat the reply as the user-facing
answer.
"""

from __future__ import annotations

import json
import re
from typing import Any

from kokoro_link.domain.value_objects.tool_call import ToolCall


_TOOL_CALL_HINT_RE = re.compile(r'\{\s*"tool"\s*:\s*"', re.DOTALL)


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
