"""Deferred proactive intent — short-lived "motive half-life" record.

When the proactive ``intention_judge`` blocks a tick despite the cheap
gate passing, the inner motive the judge identified does not simply
vanish. We persist it as a ``DeferredIntent`` with a TTL (default 24h)
and re-surface it as a fact-layer block in subsequent intention judge
calls. The next pass can then re-evaluate "is the timing right *now*?"
in light of all currently-active deferred motives, rather than the
character forgetting an authentic urge the moment one bad moment passes.

Design notes (HUMANIZATION_ROADMAP §3.4):

- **LLM-first 紅線**: this entity stores *facts* the judge produced
  (motive / purpose / risk / best-timing text). The decision whether to
  act on a re-surfaced motive belongs to the LLM, never to an if-else
  branch. We do **not** add a "score" or "priority" the dispatcher
  reads programmatically.
- **TTL is hard**: expiry is a property of the row, not a heuristic.
  Past ``expires_at`` the entity is filtered out before prompt
  injection regardless of status. Background GC then marks them.
- **Per-(character, operator)**: same isolation rule as
  ``OperatorPersona`` / ``EmotionEvent`` — a motive learned for one
  pair never bleeds into another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


STATUS_ACTIVE: Final = "active"
"""Still within TTL and has not been acted on. Re-surfaced in the next
``intention_judge`` call as a fact-layer block."""

STATUS_CONSUMED: Final = "consumed"
"""The character successfully pushed a proactive message after this
motive was active in the prompt. We mark it as folded into reality so
it stops being re-surfaced as 'pending'."""

STATUS_EXPIRED: Final = "expired"
"""Past TTL without being acted on. GC sweep moves rows here so
list/active queries don't have to recompute expiry each call."""


_VALID_STATUSES: Final = frozenset({STATUS_ACTIVE, STATUS_CONSUMED, STATUS_EXPIRED})


@dataclass(frozen=True, slots=True)
class DeferredIntent:
    """One deferred proactive motive."""

    id: str
    character_id: str
    operator_id: str
    trigger: str
    """``ProactiveTrigger`` value at the time the motive was blocked.
    Stored as a plain string to keep the entity Enum-free (mirrors how
    other open-set codes live in this layer)."""
    inner_motive: str
    conversation_purpose: str
    expected_reply: str
    risk: str
    best_timing: str
    reason: str
    """The ``intention_judge`` ``reason`` field — the LLM's own short
    explanation of why this slot was not consumed *now*."""
    status: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = field(default=None)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"DeferredIntent.status must be one of {sorted(_VALID_STATUSES)}, "
                f"got {self.status!r}",
            )
        if not self.character_id.strip():
            raise ValueError("DeferredIntent.character_id must be non-empty")
        if not self.operator_id.strip():
            raise ValueError("DeferredIntent.operator_id must be non-empty")
        if self.expires_at <= self.created_at:
            raise ValueError(
                "DeferredIntent.expires_at must be after created_at",
            )

    @classmethod
    def new(
        cls,
        *,
        character_id: str,
        operator_id: str,
        trigger: str,
        inner_motive: str,
        conversation_purpose: str = "",
        expected_reply: str = "",
        risk: str = "",
        best_timing: str = "",
        reason: str = "",
        ttl_minutes: int = 24 * 60,
        now: datetime | None = None,
    ) -> "DeferredIntent":
        ref = now or _utcnow()
        ttl = max(1, int(ttl_minutes))
        return cls(
            id=str(uuid4()),
            character_id=character_id.strip(),
            operator_id=operator_id.strip(),
            trigger=trigger.strip() or "tick",
            inner_motive=inner_motive.strip(),
            conversation_purpose=conversation_purpose.strip(),
            expected_reply=expected_reply.strip(),
            risk=risk.strip(),
            best_timing=best_timing.strip(),
            reason=reason.strip(),
            status=STATUS_ACTIVE,
            created_at=ref,
            expires_at=ref + timedelta(minutes=ttl),
        )

    def is_active_at(self, when: datetime) -> bool:
        """True iff still status=active *and* not past TTL at ``when``."""
        return self.status == STATUS_ACTIVE and when < self.expires_at

    def marked_consumed(self, *, now: datetime | None = None) -> "DeferredIntent":
        return _replace(self, status=STATUS_CONSUMED, consumed_at=now or _utcnow())

    def marked_expired(self) -> "DeferredIntent":
        return _replace(self, status=STATUS_EXPIRED)


def _replace(intent: DeferredIntent, **overrides) -> DeferredIntent:
    from dataclasses import replace
    return replace(intent, **overrides)
