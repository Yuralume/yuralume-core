"""Audit record for a single proactive evaluation.

Every time the dispatcher looks at a character and decides whether to
send an unprompted message, we write one of these rows. Even no-op
decisions (gate blocked, decider skipped) get logged so the operator
can debug why the character feels too chatty / too silent.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger


@dataclass(frozen=True, slots=True)
class ProactiveAttempt:
    id: str
    character_id: str
    trigger: ProactiveTrigger
    outcome: ProactiveOutcome
    reason: str
    decided_at: datetime
    binding_id: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def record(
        cls,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        outcome: ProactiveOutcome,
        reason: str = "",
        binding_id: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> "ProactiveAttempt":
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            trigger=trigger,
            outcome=outcome,
            reason=reason.strip(),
            decided_at=now or datetime.now(timezone.utc),
            binding_id=binding_id,
            message=message,
            metadata=dict(metadata or {}),
        )
