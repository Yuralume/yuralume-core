"""Routing-level reasoning override — shared parse/serialise rules.

LLM routing preference entries (global ``feature_models[feature_key]``
and ``feature_model_groups[group_key]``) may carry an optional
``reasoning`` object next to ``provider_id`` / ``model_id``::

    {
      "provider_id": "openai",
      "model_id": "gpt-5.2",
      "reasoning": {"reasoning_effort": "high"}
    }

When present, :class:`PreferenceBackedActiveLLMProvider` binds the
whole reasoning trio onto the resolved adapter for that call path,
replacing the provider connection's own reasoning defaults. Absent =
inherit the connection settings (pre-existing behaviour). An entry may
carry ONLY ``reasoning`` (no provider/model pin) — the model keeps
inheriting from the next layer up while the reasoning posture applies.

The raw shape is read in three places — the resolver (per call), the
startup :class:`ModelPreferenceValidator` (repair must not drop it) and
the system preference routes (API round-trip) — so the parse rules live
here once.
"""

from __future__ import annotations

from typing import Any

from kokoro_link.contracts.llm import ReasoningOverrides

REASONING_ENTRY_KEY = "reasoning"


def parse_reasoning_override(entry: Any) -> ReasoningOverrides | None:
    """Extract the reasoning override from a raw preference entry.

    Returns ``None`` for anything that doesn't amount to an explicit
    posture — missing key, malformed shapes, or an all-default object —
    so callers can treat ``None`` uniformly as "inherit connection
    settings". Malformed field values degrade to unset rather than
    raising: routing preferences are non-critical path.
    """
    if not isinstance(entry, dict):
        return None
    raw = entry.get(REASONING_ENTRY_KEY)
    if not isinstance(raw, dict):
        return None
    return reasoning_override_from_fields(
        disable_reasoning=raw.get("disable_reasoning"),
        reasoning_effort=raw.get("reasoning_effort"),
        thinking_budget_tokens=raw.get("thinking_budget_tokens"),
    )


def reasoning_override_from_fields(
    *,
    disable_reasoning: Any = None,
    reasoning_effort: Any = None,
    thinking_budget_tokens: Any = None,
) -> ReasoningOverrides | None:
    """Build a :class:`ReasoningOverrides` from loose field values.

    Shared by the raw-dict parser above and the API routes (which hold
    already-validated pydantic fields). All-default input returns
    ``None`` — an empty override is indistinguishable from "not set".
    """
    disable = disable_reasoning is True
    effort = (
        reasoning_effort.strip()
        if isinstance(reasoning_effort, str) and reasoning_effort.strip()
        else None
    )
    budget = (
        thinking_budget_tokens
        if isinstance(thinking_budget_tokens, int)
        and not isinstance(thinking_budget_tokens, bool)
        and thinking_budget_tokens > 0
        else None
    )
    if not disable and effort is None and budget is None:
        return None
    return ReasoningOverrides(
        disable_reasoning=disable,
        reasoning_effort=effort,
        thinking_budget_tokens=budget,
    )


def reasoning_pref_value(
    overrides: ReasoningOverrides | None,
) -> dict[str, object] | None:
    """Serialise an override back to the stored preference shape.

    Only explicitly-set fields are written so stored entries stay
    minimal (mirrors the connection-level "unset sends nothing" rule).
    """
    if overrides is None:
        return None
    value: dict[str, object] = {}
    if overrides.disable_reasoning:
        value["disable_reasoning"] = True
    if overrides.reasoning_effort is not None:
        value["reasoning_effort"] = overrides.reasoning_effort
    if overrides.thinking_budget_tokens is not None:
        value["thinking_budget_tokens"] = overrides.thinking_budget_tokens
    return value or None
