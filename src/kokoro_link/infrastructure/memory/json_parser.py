"""Tolerant JSON parser for LLM memory extraction output.

LLM responses often wrap JSON in code fences, prepend preambles, or
append trailing commentary. This module extracts the first balanced
JSON array from arbitrary text so the extractor can survive sloppy
formatting without forcing a specific prompt style.
"""

from __future__ import annotations

import json
from typing import Any


def parse_memory_payload(raw: str) -> list[dict[str, Any]]:
    """Return a list of dict payloads extracted from ``raw``.

    Never raises. Returns an empty list when no JSON array is found or
    when the payload is not an array of objects.
    """
    candidate = _extract_array(raw)
    if candidate is None:
        return []
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def _extract_array(text: str) -> str | None:
    """Return the first top-level JSON array substring, or ``None``.

    Walks the string with a small state machine that tracks quoting and
    escape sequences so brackets inside strings do not throw off the
    depth counter.
    """
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
