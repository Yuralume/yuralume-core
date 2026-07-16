"""Proactive-messaging introspection endpoints.

Read-only view over the audit log so operators can see why the
character has or hasn't been messaging. Creating / tuning thresholds
lives on the Character endpoints; toggling per-binding acceptance
lives on the messaging bindings endpoints.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt

router = APIRouter(tags=["proactive"])


class ProactiveAttemptResponse(BaseModel):
    id: str
    character_id: str
    trigger: str
    outcome: str
    reason: str
    binding_id: str | None = None
    message: str | None = None
    metadata: dict = Field(default_factory=dict)
    decided_at: datetime

    @classmethod
    def from_domain(cls, attempt: ProactiveAttempt) -> "ProactiveAttemptResponse":
        return cls(
            id=attempt.id,
            character_id=attempt.character_id,
            trigger=attempt.trigger.value,
            outcome=attempt.outcome.value,
            reason=attempt.reason,
            binding_id=attempt.binding_id,
            message=attempt.message,
            metadata=dict(attempt.metadata or {}),
            decided_at=attempt.decided_at,
        )


class ProactiveEvaluateResponse(BaseModel):
    ok: bool
    attempt: ProactiveAttemptResponse | None = None
    message: str | None = None


@router.get(
    "/characters/{character_id}/proactive/attempts",
    response_model=list[ProactiveAttemptResponse],
)
async def list_attempts(
    character_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[ProactiveAttemptResponse]:
    repo = container.proactive_attempt_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proactive messaging is not wired",
        )
    attempts = await repo.list_for_character(character_id, limit=limit)
    return [ProactiveAttemptResponse.from_domain(a) for a in attempts]


@router.post(
    "/characters/{character_id}/proactive/evaluate",
    response_model=ProactiveEvaluateResponse,
)
async def evaluate_now(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> ProactiveEvaluateResponse:
    """Force an immediate proactive evaluation.

    Useful for debugging: press this button in the UI and watch the
    resulting attempt show up in the log. Respects the gate / decider
    just like a normal tick.
    """
    dispatcher = container.proactive_dispatcher
    if dispatcher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proactive dispatcher is not wired",
        )
    from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

    attempt = await dispatcher.evaluate(
        character_id=character_id, trigger=ProactiveTrigger.TICK,
    )
    return ProactiveEvaluateResponse(
        ok=True, attempt=ProactiveAttemptResponse.from_domain(attempt),
    )
