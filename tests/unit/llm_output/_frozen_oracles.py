"""Frozen copies of the extractors `kokoro_link.llm_output` replaced.

These functions are **dead code on purpose**. They are byte-for-byte
the implementations that lived in

- ``application/services/tool_call_parser.py``
  (``_extract_first_object``, ``_repair_truncated_object``,
  ``_balanced_end``, ``_iter_embedded_json``, ``_FENCE_RE``)
- ``infrastructure/memory/json_parser.py``
  (``_extract_array``, ``parse_memory_payload``)
- ``infrastructure/post_turn/llm_processor.py`` (``_extract_object``)

as of the commit before DH1, and they exist so the differential tests
have an oracle that cannot drift when the shared layer changes.

**Never "fix" anything in this file.** A bug preserved here is the
point: if the old code mis-parsed something, the new layer is allowed
to parse it correctly (the assertion is one-directional — new ⊇ old),
but the record of what the old code actually did must stay honest. If
you find yourself editing these, you are editing the evidence.

The corresponding production functions are gone, so nothing imports
these except the differential tests.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any


# --- from tool_call_parser.py -----------------------------------------

FROZEN_TOOL_CALL_HINT_RE = re.compile(r'\{\s*"tool"\s*:\s*"', re.DOTALL)

FROZEN_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*|\s*```$")

_CLOSERS: dict[str, str] = {"{": "}", "[": "]"}

FROZEN_MAX_SHAPE_SCAN_CHARS = 8000


def frozen_looks_like_tool_call_attempt(raw: str) -> bool:
    if not raw:
        return False
    return FROZEN_TOOL_CALL_HINT_RE.search(raw) is not None


def frozen_balanced_end(text: str, start: int) -> int | None:
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


def frozen_iter_embedded_json(text: str) -> Iterator[Any]:
    index = 0
    length = len(text)
    while index < length:
        if text[index] not in _CLOSERS:
            index += 1
            continue
        end = frozen_balanced_end(text, index)
        if end is None:
            index += 1
            continue
        try:
            yield json.loads(text[index : end + 1])
        except json.JSONDecodeError:
            pass
        index = end + 1


def frozen_extract_first_object(text: str) -> dict[str, Any] | None:
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


def frozen_repair_truncated_object(text: str) -> dict[str, Any] | None:
    if not frozen_looks_like_tool_call_attempt(text):
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    last_key_comma = -1
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
        return None
    suffix = ""
    if in_string:
        if text.endswith("\\"):
            suffix += "\\"
        suffix += '"'
    candidate = text[start:] + suffix
    candidate = candidate.rstrip()
    if candidate.endswith(","):
        candidate = candidate[:-1]
    candidate += "}" * depth
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        _ = last_key_comma
        return None
    return parsed if isinstance(parsed, dict) else None


def frozen_tool_call_object(raw: str) -> dict[str, Any] | None:
    """The composite extraction step of the old ``parse_tool_call``.

    Everything below ``obj is None`` in that function was schema policy
    (the ``tool`` / ``args`` contract) and did not move, so the
    migration risk is confined to exactly these two calls.
    """
    obj = frozen_extract_first_object(raw)
    if obj is None:
        obj = frozen_repair_truncated_object(raw)
    return obj


def frozen_looks_like_object_literal(raw: str) -> bool:
    if not raw:
        return False
    text = FROZEN_FENCE_RE.sub("", raw.strip()).strip()
    if len(text) < 2:
        return False
    if text.startswith("{") and text.endswith("}"):
        return True
    return text.startswith("[") and text.endswith("]") and "{" in text


def _frozen_is_container(value: Any) -> bool:
    if isinstance(value, dict):
        return True
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) for item in value
    )


def _frozen_is_call_shaped(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value) and all(isinstance(item, dict) for item in value)
    if not isinstance(value, dict):
        return False
    return any(_frozen_is_container(item) for item in value.values())


def frozen_looks_like_tool_call_shape(raw: str) -> bool:
    if not raw:
        return False
    if frozen_looks_like_object_literal(raw):
        return True
    text = FROZEN_FENCE_RE.sub("", raw.strip())[:FROZEN_MAX_SHAPE_SCAN_CHARS]
    return any(_frozen_is_call_shaped(value) for value in frozen_iter_embedded_json(text))


# --- from infrastructure/memory/json_parser.py ------------------------


def frozen_extract_array_text(text: str) -> str | None:
    start = text.find("[")
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
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def frozen_parse_memory_payload(raw: str) -> list[dict[str, Any]]:
    candidate = frozen_extract_array_text(raw)
    if candidate is None:
        return []
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def frozen_extract_array(raw: str) -> list[Any] | None:
    """The old array extraction expressed as a value, for the ⊇ check."""
    candidate = frozen_extract_array_text(raw)
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


# --- from infrastructure/post_turn/llm_processor.py -------------------


def frozen_post_turn_extract_object(text: str) -> dict[str, Any] | None:
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
