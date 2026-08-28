"""Tolerant JSON parser for LLM memory extraction output.

LLM responses often wrap JSON in code fences, prepend preambles, or
append trailing commentary. This module extracts the first balanced
JSON array from arbitrary text so the extractor can survive sloppy
formatting without forcing a specific prompt style.

The balance scanning itself lives in ``kokoro_link.llm_output``; what
remains here is the contract this parser owns — "an array of objects,
and nothing else counts".

Also used by the schedule planner and the weather-drift adjuster, whose
prompts share the same array-of-objects shape.
"""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.llm_output import extract_array_outcome, log_parse_outcome


_LOGGER = logging.getLogger(__name__)


def parse_memory_payload(raw: str) -> list[dict[str, Any]]:
    """Return a list of dict payloads extracted from ``raw``.

    Never raises. Returns an empty list when no JSON array is found or
    when the payload is not an array of objects.

    Every caller's prompt demands an array — an empty one when there is
    nothing to report — so *any* failure here is the model ignoring the
    contract, and worth a warning rather than the silent ``[]`` this
    used to return. The return value is unchanged.
    """
    outcome = extract_array_outcome(raw)
    log_parse_outcome(_LOGGER, outcome, site="memory.parse_memory_payload")
    if outcome.value is None:
        return []
    return [entry for entry in outcome.value if isinstance(entry, dict)]
