"""Routing-level vision override — shared tri-state parse rule.

LLM routing preference entries (global ``feature_models[feature_key]``,
``feature_model_groups[group_key]`` and the primary ``active_model``)
may carry an optional ``supports_vision`` bool next to
``provider_id`` / ``model_id``::

    {"provider_id": "openrouter", "model_id": "openai/gpt-4o",
     "supports_vision": true}

Why per-route, not just per-connection: one aggregator connection
(OpenRouter) fronts BOTH vision and text-only models, so a single
connection-level ``supports_vision`` flag is inherently insufficient —
the same connection needs to attach images for a vision route and drop
them for a text-only route. A routing entry overrides the connection
flag for calls resolved through it.

Tri-state semantics — the return value distinguishes "operator pinned a
value" from "operator said nothing":

* an explicit ``True`` / ``False`` bool → that value (override the
  connection flag for this route),
* anything else — key absent, ``None``, string, number, malformed
  entry → ``None`` (= inherit the connection-level flag).

Only a real ``bool`` counts as an assertion; a stringy/numeric truthy
value is deliberately NOT coerced (the repo forbids keyword/string
special-casing, and a coerced guess would silently mis-attach images).
"""

from __future__ import annotations

VISION_ENTRY_KEY = "supports_vision"


def parse_vision_override(entry: dict | None) -> bool | None:
    """Extract the vision override from a raw preference entry.

    Returns the pinned ``bool`` when present, else ``None`` (inherit the
    connection flag). Kept tiny and shared so the resolver (per call),
    the startup validator (repair must not drop it) and the API routes
    (round-trip) all read the field the same way.
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get(VISION_ENTRY_KEY)
    # ``bool`` is checked before any int/str coercion: ``isinstance(1,
    # bool)`` is False, so a bare number never masquerades as a pin.
    if isinstance(value, bool):
        return value
    return None
