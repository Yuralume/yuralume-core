"""Observability helpers for conversational persona discovery."""

from __future__ import annotations

from typing import Any

from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan


def persona_curiosity_plan_summary(
    plan: PersonaCuriosityPlan | None,
    *,
    surface: str,
) -> dict[str, Any] | None:
    """Return a compact, stable debug summary for turn/proactive refs."""
    if plan is None:
        return None
    metadata = dict(plan.planner_metadata or {})
    return {
        "surface": surface,
        "should_ask": plan.should_ask,
        "target_layer": plan.target_layer,
        "target_topic": plan.target_topic,
        "tone_strategy": plan.tone_strategy,
        "question_intent": plan.question_intent,
        "safety_reason": plan.safety_reason,
        "avoid": list(plan.avoid),
        "provider_id": metadata.get("provider_id", ""),
        "model_id": metadata.get("model_id", ""),
        "latency_ms": metadata.get("latency_ms"),
        "prompt_tokens": metadata.get("prompt_tokens"),
        "completion_tokens": metadata.get("completion_tokens"),
        "error": metadata.get("error"),
        "recent_attempt_count": int(metadata.get("recent_attempt_count") or 0),
    }
