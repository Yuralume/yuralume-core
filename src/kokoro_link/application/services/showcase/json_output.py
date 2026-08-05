"""Fence-tolerant JSON coercion for the showcase LLM steps.

Models wrap JSON in prose or code fences often enough that tolerating it
is cheaper than losing a post to a stray ```` ```json ````. Kept in one
module because both the reviewer and the translator need exactly this
and nothing more; a shared parser also means a model that starts
misbehaving is fixed in one place.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence


def coerce_json_object(raw: str) -> Mapping[str, object] | None:
    """Best-effort parse of a JSON object out of a model answer.

    Returns ``None`` rather than raising: every caller here is fail-soft,
    and "the model did not answer in the shape we asked for" is a normal
    outcome that must degrade to manual review, never to an exception
    that kills a batch.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        # ```json\n{...}\n``` — drop the fence lines, keep the middle.
        lines = [line for line in text.splitlines() if not line.startswith("```")]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, Mapping) else None


def as_reason_list(value: object) -> list[str]:
    """Normalise whatever the model put in ``reasons`` into a string list.

    Models answer with a list, a single string, or occasionally a dict of
    checklist items. All three are usable to a human reading the review;
    none of them is worth failing a review over.
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        return [f"{key}: {item}" for key, item in value.items() if item]
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


__all__ = ["as_reason_list", "coerce_json_object"]
