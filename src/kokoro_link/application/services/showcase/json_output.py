"""Fence-tolerant JSON coercion for the showcase LLM steps.

Models wrap JSON in prose or code fences often enough that tolerating it
is cheaper than losing a post to a stray ```` ```json ````. Kept in one
module because both the reviewer and the translator need exactly this
and nothing more; a shared parser also means a model that starts
misbehaving is fixed in one place.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence

from kokoro_link.llm_output import (
    extract_object_outcome,
    first_region_is_array,
    log_parse_outcome,
)

_LOGGER = logging.getLogger(__name__)


def _whole_text_parses_to_non_dict(text: str) -> bool:
    """True when ``text`` is, on its own, complete valid JSON that is
    not an object. The old code's first step was a whole-text
    ``json.loads``: when that succeeded with a non-dict value (the
    model wrapped its answer in ``[...]``), the final
    ``isinstance(payload, Mapping)`` check discarded it *without* ever
    trying the crude fallback slice (that branch is reached only via
    the whole-parse *raising*, not via it succeeding at the wrong
    type).

    Note what this cannot do, and why it is not the array guard on its
    own: it needs the *whole* reply to decode, so it says nothing about
    a reply with trailing commentary. ``first_region_is_array`` covers
    that half — see ``coerce_json_object``.
    """
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        # RecursionError: a model stuck in a repetition loop emits
        # thousands of nested openers and ``json.loads`` blows its
        # C-stack guard rather than reporting a decode error.
        return False
    return not isinstance(value, dict)


def _crude_object_span_decodes(text: str) -> bool:
    """Old behaviour, preserved exactly: does the first-``{`` to
    last-``}`` slice — the old code's ``except`` fallback — parse as
    JSON at all. When it does, old already succeeded with exactly this
    value (most commonly a single object nested one level inside an
    otherwise-scalar wrapper), so the shared scanner recovers it
    identically. Used only as a gate; see ``coerce_json_object``.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        json.loads(text[start: end + 1])
    except (json.JSONDecodeError, RecursionError):
        return False
    return True


def coerce_json_object(
    raw: str, *, site: str = "showcase.json_output",
) -> Mapping[str, object] | None:
    """Best-effort parse of a JSON object out of a model answer.

    Returns ``None`` rather than raising: every caller here is fail-soft,
    and "the model did not answer in the shape we asked for" is a normal
    outcome that must degrade to manual review, never to an exception
    that kills a batch.

    DH2-services: delegates to the shared scanner (fence-agnostic
    balanced-brace scan, so the old "drop any ``` line" pre-pass is
    redundant with it) with truncation repair on — every prompt behind
    this helper unconditionally asks for a JSON object, so a reply
    chopped by ``max_tokens`` is now worth trying to salvage rather than
    falling straight to manual review. ``site`` distinguishes which of
    the four callers (reviewer, translator, image reviewer, official-card
    translator) a failure came from in the shared log.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.startswith("```")]
        text = "\n".join(lines).strip()

    # Old's control flow, preserved: whole-text parse first (only a
    # *leading* fence gets dropped before this point, matching the old
    # code exactly); a non-dict success there is a dead end old never
    # walked past for the crude fallback.
    if _whole_text_parses_to_non_dict(text):
        return None
    # A stray *trailing*-only fence (no leading one, so the drop above
    # never ran) makes the whole-text parse above fail for a reason
    # that has nothing to do with truncation — old's own crude slice
    # ignores it too (fences never land between the braces it looks
    # for), so it still finds and unwraps a lone embedded object here.
    # Only when *that* also fails do we ask whether the reply is
    # array-shaped (several sibling objects in a list) rather than a
    # truncated fragment repair should still get a shot at. FX1/DH-2:
    # asked structurally, because the old spelling of this second guard
    # was another whole-text parse — and the reply that most needs
    # catching here, a closed array with a sentence after it, is exactly
    # the one a whole-text parse cannot see.
    if not _crude_object_span_decodes(text) and first_region_is_array(text):
        return None

    outcome = extract_object_outcome(raw)
    log_parse_outcome(_LOGGER, outcome, site=site)
    return outcome.value


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
