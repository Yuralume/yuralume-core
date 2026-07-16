"""Audit record for a single LLM turn (chat / proactive / post-turn / etc).

Whereas ``TurnJournal`` snapshots *pre-turn state* for undo, this entity
captures *what happened during the turn*: the fully-assembled prompt,
the raw model output, latency / token usage, and refs to any side
effects (memories added, state changes, proactive attempts).

Recorded for every turn regardless of outcome — including proactive
evaluations that the gate blocked (no LLM call) so the observability
dashboard can show the full funnel. Replay / evals harness reads these
back via the repository port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


TurnKind = str
"""Free-form string tag. Known values:

* ``chat`` — user → assistant streaming turn
* ``proactive`` — character-initiated push (gate / intention / decider)
* ``post_turn_processor`` — extraction LLM call after a chat turn
* ``idle_drift`` — idle-window state drift LLM call
* ``dream`` — quiet-hours consolidation LLM call
* ``planner`` — daily schedule planner LLM call

Open set so new subsystems can record without touching domain code.
"""


@dataclass(frozen=True, slots=True)
class TurnRecord:
    id: str
    character_id: str
    conversation_id: str | None
    """``None`` for proactive / dream / planner / idle_drift — those are
    not anchored to a specific conversation."""
    kind: TurnKind
    model_id: str
    """Identifier of the model that produced ``response_text`` (e.g.
    ``claude-opus-4-7`` or LM Studio model name). Empty string when the
    turn was rejected before any LLM call (proactive gate block)."""
    prompt_assembled: str
    """Full prompt text as sent to the model. Empty when no LLM call
    happened (gate-blocked proactive)."""
    response_text: str
    """Raw model output. Empty when no LLM call or on error."""
    prompt_pack_hash: str = ""
    """Stable hash of the effective prompt pack + prompt-affecting flags."""
    response_json: dict[str, Any] | None = None
    """Parsed structured output for turns that produced JSON
    (post-turn processor, intention judge, decider, dream)."""
    latency_ms: int | None = None
    """Wall-clock latency of the LLM call. ``None`` when no LLM call."""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    """Error message when the LLM call failed. ``None`` on success."""
    post_turn_refs: dict[str, Any] = field(default_factory=dict)
    """Refs to side effects produced by this turn — opaque dict so each
    kind can record what's relevant. Examples:

    * chat: ``{memory_ids: [...], state_change_id: ..., emotion_event_ids: [...]}``
    * proactive: ``{proactive_attempt_id: ..., gate_verdict: ..., decider_verdict: ...}``
    """
    operator_feedback: dict[str, Any] = field(default_factory=dict)
    """Owner/operator eval feedback attached after the turn lands.

    Shape is intentionally open for future workflows, but the admin API
    currently writes ``{kind, note, tags, source, updated_at}``.
    """
    created_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def new(
        cls,
        *,
        character_id: str,
        kind: TurnKind,
        id: str | None = None,
        model_id: str = "",
        prompt_pack_hash: str = "",
        prompt_assembled: str = "",
        response_text: str = "",
        conversation_id: str | None = None,
        response_json: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        error: str | None = None,
        post_turn_refs: dict[str, Any] | None = None,
        operator_feedback: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> TurnRecord:
        return cls(
            id=id or str(uuid4()),
            character_id=character_id,
            conversation_id=conversation_id,
            kind=kind,
            model_id=model_id,
            prompt_pack_hash=prompt_pack_hash,
            prompt_assembled=prompt_assembled,
            response_text=response_text,
            response_json=dict(response_json) if response_json else None,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=error,
            post_turn_refs=dict(post_turn_refs or {}),
            operator_feedback=dict(operator_feedback or {}),
            created_at=now or _utcnow(),
        )
