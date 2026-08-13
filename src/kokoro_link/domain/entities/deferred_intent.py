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


def _as_utc(value: datetime) -> datetime:
    """Normalise to aware UTC. A naive value is read as UTC — the same
    tolerance the persistence layer applies on the way back out, and it
    keeps ``created_at`` / ``expires_at`` / ``revisit_at`` mutually
    comparable no matter which caller supplied which."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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

REVISIT_GRACE_MINUTES: Final = 120
"""How long a row outlives its own alarm when the appointment falls past
the ordinary TTL.

An alarm can only ring while its row is still live — every due query is
``expires_at > now AND revisit_at <= now``. Without this floor, any
appointment further out than the TTL ("明晚八點再一起", 25h away on a 24h
TTL) would be born dead: stored, never queryable, silently dropped. The
grace window on top of the alarm itself covers the gap between the
appointment and the tick that notices it (scheduler interval, a restart,
a night-hours block that defers the look until morning)."""


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
    revisit_at: datetime | None = field(default=None)
    """UTC instant the judge itself named as "the moment this becomes
    appropriate" (an agreed 19:30, "after 21:00 I'm free"). ``None`` —
    the ordinary case — means the motive has no clock attached and
    behaves exactly as it did before this field existed.

    It is an **alarm, not a decision**: once due it buys the tick one
    cooldown exemption so the judge gets to look again, and the judge
    still owns the answer. Cleared the moment it is spent (see
    ``without_revisit``) so one parked motive can't exempt every
    subsequent tick."""

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
        revisit_at: datetime | None = None,
        ttl_minutes: int = 24 * 60,
        now: datetime | None = None,
    ) -> "DeferredIntent":
        ref = _as_utc(now or _utcnow())
        ttl = max(1, int(ttl_minutes))
        expires_at = ref + timedelta(minutes=ttl)
        if revisit_at is not None:
            revisit_at = _as_utc(revisit_at)
            # Never shortens: the TTL is the floor, the alarm's own
            # horizon only ever pushes it out.
            expires_at = max(
                expires_at,
                revisit_at + timedelta(minutes=REVISIT_GRACE_MINUTES),
            )
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
            expires_at=expires_at,
            revisit_at=revisit_at,
        )

    def is_active_at(self, when: datetime) -> bool:
        """True iff still status=active *and* not past TTL at ``when``."""
        return self.status == STATUS_ACTIVE and when < self.expires_at

    def is_due_at(self, when: datetime) -> bool:
        """True iff still live at ``when`` *and* its alarm has rung."""
        return (
            self.is_active_at(when)
            and self.revisit_at is not None
            and self.revisit_at <= when
        )

    def without_revisit(self) -> "DeferredIntent":
        """Drop the alarm, keeping the motive itself parked."""
        return _replace(self, revisit_at=None)

    def with_revisit(self, revisit_at: datetime) -> "DeferredIntent":
        """Put an alarm back on a parked motive.

        Used to undo a spend that bought a tick which then failed to
        produce any judgement at all — see ``restore_revisit`` on the
        repository port."""
        return _replace(self, revisit_at=_as_utc(revisit_at))

    def marked_consumed(self, *, now: datetime | None = None) -> "DeferredIntent":
        return _replace(self, status=STATUS_CONSUMED, consumed_at=now or _utcnow())

    def marked_expired(self) -> "DeferredIntent":
        return _replace(self, status=STATUS_EXPIRED)


def _replace(intent: DeferredIntent, **overrides) -> DeferredIntent:
    from dataclasses import replace
    return replace(intent, **overrides)
