"""Observability dashboard endpoints.

Read-only surface over the data Phase 1–3 populate:

* ``TurnRecord`` — every LLM turn (chat / proactive / post-turn / ...).
* ``EmotionEvent`` — per-cause emotion deltas behind the aggregator.

Designed to back the admin panel (`frontend/src/components/observability/`)
but also useful from CLI / curl for debugging "why did the character
just do that". Mounted under ``/api/v1/admin/observability``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, get_current_user, require_admin
from kokoro_link.application.services.nsfw_mode import CONTENT_MODE_NSFW
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID, OperatorProfile
from kokoro_link.domain.entities.persona_curiosity import PersonaCuriosityAttempt
from kokoro_link.domain.entities.turn_record import TurnRecord

_ALLOWED_OPERATOR_FEEDBACK_KINDS = {"out_of_character", "felt_human"}

# Admin-only surface (P0-4 / P1-1 in the auth review): every endpoint
# on this router requires the bearer token to belong to an admin user.
# Dependencies set at the router level rather than per-endpoint so a
# new route can't be added without inheriting the guard.
router = APIRouter(
    tags=["observability"], dependencies=[Depends(require_admin)],
)


# ---------- response models ---------------------------------------------------


class TurnRecordSummary(BaseModel):
    """Light list-row — omits ``prompt_assembled`` to keep payloads small."""

    id: str
    character_id: str
    conversation_id: str | None = None
    kind: str
    model_id: str = ""
    prompt_pack_hash: str = ""
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    operator_feedback: dict[str, Any] = Field(default_factory=dict)
    response_excerpt: str
    created_at: datetime

    @classmethod
    def from_domain(cls, record: TurnRecord) -> "TurnRecordSummary":
        return cls(
            id=record.id,
            character_id=record.character_id,
            conversation_id=record.conversation_id,
            kind=record.kind,
            model_id=record.model_id,
            prompt_pack_hash=record.prompt_pack_hash,
            latency_ms=record.latency_ms,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            error=record.error,
            operator_feedback=record.operator_feedback,
            response_excerpt=record.response_text[:200],
            created_at=record.created_at,
        )


class TurnRecordDetail(BaseModel):
    """Full row including assembled prompt + response. Returned by the
    single-record endpoint only — list endpoint uses the summary above."""

    id: str
    character_id: str
    conversation_id: str | None = None
    kind: str
    model_id: str = ""
    prompt_pack_hash: str = ""
    prompt_assembled: str = ""
    response_text: str = ""
    response_json: dict[str, Any] | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    post_turn_refs: dict[str, Any] = Field(default_factory=dict)
    operator_feedback: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @classmethod
    def from_domain(cls, record: TurnRecord) -> "TurnRecordDetail":
        return cls(
            id=record.id,
            character_id=record.character_id,
            conversation_id=record.conversation_id,
            kind=record.kind,
            model_id=record.model_id,
            prompt_pack_hash=record.prompt_pack_hash,
            prompt_assembled=record.prompt_assembled,
            response_text=record.response_text,
            response_json=record.response_json,
            latency_ms=record.latency_ms,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            error=record.error,
            post_turn_refs=record.post_turn_refs,
            operator_feedback=record.operator_feedback,
            created_at=record.created_at,
        )


class OperatorFeedbackUpdate(BaseModel):
    kind: str = Field(
        description="One of: out_of_character, felt_human.",
    )
    note: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class LatencyBucketResponse(BaseModel):
    lower_ms: int
    upper_ms: int | None
    count: int


class EmotionEventResponse(BaseModel):
    id: str
    character_id: str
    operator_id: str
    cause_ref_kind: str
    cause_ref_id: str | None = None
    valence: float
    arousal: float
    intensity: float
    affection_delta: int
    fatigue_delta: int
    trust_delta: int
    energy_delta: int
    emotion_label: str
    evidence_quote: str
    decay_half_life_minutes: int
    created_at: datetime

    @classmethod
    def from_domain(cls, event: EmotionEvent) -> "EmotionEventResponse":
        return cls(
            id=event.id,
            character_id=event.character_id,
            operator_id=event.operator_id,
            cause_ref_kind=event.cause_ref_kind,
            cause_ref_id=event.cause_ref_id,
            valence=event.valence,
            arousal=event.arousal,
            intensity=event.intensity,
            affection_delta=event.affection_delta,
            fatigue_delta=event.fatigue_delta,
            trust_delta=event.trust_delta,
            energy_delta=event.energy_delta,
            emotion_label=event.emotion_label,
            evidence_quote=event.evidence_quote,
            decay_half_life_minutes=event.decay_half_life_minutes,
            created_at=event.created_at,
        )


class ProactiveFunnelResponse(BaseModel):
    sent: int
    decider_skipped: int
    intention_skipped: int
    gate_blocked: int
    errored: int
    disabled: int
    no_binding: int
    total: int


class PersonaCuriosityAttemptResponse(BaseModel):
    id: str
    character_id: str
    operator_id: str
    conversation_id: str | None = None
    surface: str
    target_layer: int
    target_topic: str
    question_intent: str
    status: str
    created_at: datetime
    cooldown_until: datetime | None = None
    response_turn_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_domain(
        cls,
        attempt: PersonaCuriosityAttempt,
    ) -> "PersonaCuriosityAttemptResponse":
        return cls(
            id=attempt.id,
            character_id=attempt.character_id,
            operator_id=attempt.operator_id,
            conversation_id=attempt.conversation_id,
            surface=attempt.surface,
            target_layer=attempt.target_layer,
            target_topic=attempt.target_topic,
            question_intent=attempt.question_intent,
            status=attempt.status,
            created_at=attempt.created_at,
            cooldown_until=attempt.cooldown_until,
            response_turn_id=attempt.response_turn_id,
            metadata=dict(attempt.metadata or {}),
        )


class PersonaCuriosityMetricsResponse(BaseModel):
    window_hours: int
    plan_count: int
    ask_plan_count: int
    no_ask_plan_count: int
    asked_count: int
    answered_count: int
    deflected_count: int
    ignored_count: int
    answered_ratio: float
    deflected_ratio: float
    ignored_ratio: float
    persona_candidate_facts_after_curiosity: int
    repeated_question_guard_incidents: int


class NsfwModeMetricsResponse(BaseModel):
    window_hours: int
    sampled_turns: int
    nsfw_turns: int
    normal_turns: int
    nsfw_turn_ratio: float
    current_active: bool
    current_configured: bool
    ttl_seconds: int
    enable_count: int
    manual_disable_count: int
    idle_expired_count: int
    average_active_seconds: int | None = None
    last_enabled_at: datetime | None = None
    last_disabled_at: datetime | None = None
    last_expired_at: datetime | None = None


# ---------- helpers -----------------------------------------------------------


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid ISO datetime: {value!r}",
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _turn_record_content_mode(record: TurnRecord) -> str:
    raw = record.post_turn_refs.get("content_mode")
    return str(raw).strip().lower() if raw is not None else ""


# ---------- endpoints ---------------------------------------------------------


@router.get(
    "/admin/observability/turns",
    response_model=list[TurnRecordSummary],
)
async def list_turns(
    character_id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    feedback_kind: str | None = Query(default=None),
    since: str | None = Query(
        default=None,
        description="ISO 8601 datetime — only return turns at or after this time.",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    container: ServiceContainer = Depends(get_container),
) -> list[TurnRecordSummary]:
    repo = container.turn_record_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Turn record repository is not wired",
        )
    records = await repo.list_recent(
        character_id=character_id,
        kind=kind,
        since=_parse_since(since),
        operator_feedback_kind=feedback_kind,
        limit=limit,
    )
    return [TurnRecordSummary.from_domain(r) for r in records]


# NOTE: this static path must be declared BEFORE the dynamic
# ``/turns/{turn_id}`` route below — FastAPI matches in declaration
# order and the dynamic route would otherwise capture "latency-histogram"
# as a turn id and 404 every call.
@router.get(
    "/admin/observability/turns/latency-histogram",
    response_model=list[LatencyBucketResponse],
)
async def latency_histogram(
    character_id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    since: str | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
) -> list[LatencyBucketResponse]:
    repo = container.turn_record_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Turn record repository is not wired",
        )
    buckets = await repo.latency_histogram(
        character_id=character_id,
        kind=kind,
        since=_parse_since(since),
    )
    return [
        LatencyBucketResponse(
            lower_ms=b.lower_ms, upper_ms=b.upper_ms, count=b.count,
        )
        for b in buckets
    ]


@router.get(
    "/admin/observability/metrics/nsfw-mode",
    response_model=NsfwModeMetricsResponse,
)
async def nsfw_mode_metrics(
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
    current_user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
) -> NsfwModeMetricsResponse:
    service = getattr(container, "nsfw_mode_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NSFW mode service is not wired",
        )
    repo = container.turn_record_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Turn record repository is not wired",
        )

    usage = await service.usage_metrics(user_id=current_user.id)
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    records = await repo.list_recent(since=since, limit=500)
    nsfw_turns = sum(
        1 for record in records
        if _turn_record_content_mode(record) == CONTENT_MODE_NSFW
    )
    normal_turns = 0
    for record in records:
        content_mode = _turn_record_content_mode(record)
        if content_mode and content_mode != CONTENT_MODE_NSFW:
            normal_turns += 1
    sampled_turns = len(records)
    return NsfwModeMetricsResponse(
        window_hours=window_hours,
        sampled_turns=sampled_turns,
        nsfw_turns=nsfw_turns,
        normal_turns=normal_turns,
        nsfw_turn_ratio=nsfw_turns / sampled_turns if sampled_turns else 0.0,
        current_active=usage.active,
        current_configured=usage.configured,
        ttl_seconds=usage.ttl_seconds,
        enable_count=usage.enable_count,
        manual_disable_count=usage.manual_disable_count,
        idle_expired_count=usage.idle_expired_count,
        average_active_seconds=usage.average_active_seconds,
        last_enabled_at=usage.last_enabled_at,
        last_disabled_at=usage.last_disabled_at,
        last_expired_at=usage.last_expired_at,
    )


@router.get(
    "/admin/observability/turns/{turn_id}",
    response_model=TurnRecordDetail,
)
async def get_turn(
    turn_id: str,
    container: ServiceContainer = Depends(get_container),
) -> TurnRecordDetail:
    repo = container.turn_record_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Turn record repository is not wired",
        )
    record = await repo.get(turn_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No turn record with id={turn_id!r}",
        )
    return TurnRecordDetail.from_domain(record)


@router.put(
    "/admin/observability/turns/{turn_id}/operator-feedback",
    response_model=TurnRecordDetail,
)
async def update_turn_operator_feedback(
    turn_id: str,
    payload: OperatorFeedbackUpdate,
    container: ServiceContainer = Depends(get_container),
) -> TurnRecordDetail:
    repo = container.turn_record_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Turn record repository is not wired",
        )
    kind = payload.kind.strip()
    if kind not in _ALLOWED_OPERATOR_FEEDBACK_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "operator feedback kind must be one of: "
                + ", ".join(sorted(_ALLOWED_OPERATOR_FEEDBACK_KINDS))
            ),
        )
    feedback = {
        "kind": kind,
        "note": payload.note.strip(),
        "tags": [tag.strip() for tag in payload.tags if tag.strip()],
        "source": "operator",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    record = await repo.update_operator_feedback(turn_id, feedback)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No turn record with id={turn_id!r}",
        )
    return TurnRecordDetail.from_domain(record)


@router.get(
    "/admin/observability/proactive/funnel",
    response_model=ProactiveFunnelResponse,
)
async def proactive_funnel(
    character_id: str | None = Query(default=None),
    since_hours: int = Query(default=24, ge=1, le=24 * 30),
    container: ServiceContainer = Depends(get_container),
) -> ProactiveFunnelResponse:
    """Aggregate proactive-attempt outcomes over the last N hours.

    Aggregation is done in Python over a single ``list_for_character``
    query — fine for the realistic data volume (low hundreds per
    character per day). Migrating to SQL ``GROUP BY`` is a follow-up
    when one operator's dashboard gets visibly slow.
    """
    repo = container.proactive_attempt_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proactive subsystem is not wired",
        )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    # No character_id-less query method exists on the port; loop the
    # known repository's characters via container instead. When character
    # filter is provided we use the existing per-character list.
    counts: dict[str, int] = {}
    if character_id is not None:
        attempts = await repo.list_for_character(character_id, limit=500)
    else:
        # When no character filter, we don't have a "list all" method on
        # the port. Return zeros + total=0 rather than 500. Callers that
        # care about the funnel for a specific character pass character_id;
        # cross-character aggregation can be added when there's a real
        # consumer for it.
        attempts = []
    for attempt in attempts:
        if attempt.decided_at < cutoff:
            continue
        counts[attempt.outcome.value] = counts.get(attempt.outcome.value, 0) + 1
    total = sum(counts.values())
    return ProactiveFunnelResponse(
        sent=counts.get("sent", 0),
        decider_skipped=counts.get("decider_skipped", 0),
        intention_skipped=counts.get("intention_skipped", 0),
        gate_blocked=counts.get("gate_blocked", 0),
        errored=counts.get("errored", 0),
        disabled=counts.get("disabled", 0),
        no_binding=counts.get("no_binding", 0),
        total=total,
    )


@router.get(
    "/admin/observability/persona-curiosity/attempts",
    response_model=list[PersonaCuriosityAttemptResponse],
)
async def list_persona_curiosity_attempts(
    character_id: str = Query(...),
    operator_id: str = Query(default=DEFAULT_OPERATOR_ID),
    limit: int = Query(default=50, ge=1, le=500),
    container: ServiceContainer = Depends(get_container),
) -> list[PersonaCuriosityAttemptResponse]:
    service = container.persona_curiosity_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persona curiosity service is not wired",
        )
    attempts = await service.list_recent_attempts(
        character_id,
        operator_id,
        limit=limit,
    )
    return [PersonaCuriosityAttemptResponse.from_domain(a) for a in attempts]


@router.get(
    "/admin/observability/metrics/persona-curiosity",
    response_model=PersonaCuriosityMetricsResponse,
)
async def persona_curiosity_metrics(
    character_id: str = Query(...),
    operator_id: str = Query(default=DEFAULT_OPERATOR_ID),
    since_hours: int = Query(default=72, ge=1, le=24 * 30),
    container: ServiceContainer = Depends(get_container),
) -> PersonaCuriosityMetricsResponse:
    service = container.persona_curiosity_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persona curiosity service is not wired",
        )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    attempts = [
        a for a in await service.list_recent_attempts(
            character_id,
            operator_id,
            limit=500,
        )
        if a.created_at >= cutoff
    ]
    status_counts: dict[str, int] = {}
    candidate_count = 0
    for attempt in attempts:
        status_counts[attempt.status] = status_counts.get(attempt.status, 0) + 1
        raw_candidate_ids = (attempt.metadata or {}).get(
            "persona_candidate_fact_ids",
        )
        if isinstance(raw_candidate_ids, list):
            candidate_count += len(raw_candidate_ids)

    plan_count = 0
    ask_plan_count = 0
    no_ask_plan_count = 0
    repeated_guard_incidents = 0
    turn_repo = container.turn_record_repository
    if turn_repo is not None:
        turns = await turn_repo.list_recent(
            character_id=character_id,
            kind=None,
            since=cutoff,
            limit=5000,
        )
        for turn in turns:
            summary = (turn.post_turn_refs or {}).get("persona_curiosity")
            if not isinstance(summary, dict):
                continue
            plan_count += 1
            should_ask = summary.get("should_ask") is True
            if should_ask:
                ask_plan_count += 1
            else:
                no_ask_plan_count += 1
                if int(summary.get("recent_attempt_count") or 0) > 0:
                    repeated_guard_incidents += 1

    answered = status_counts.get("answered", 0)
    deflected = status_counts.get("deflected", 0)
    ignored = status_counts.get("ignored", 0)
    terminal_total = answered + deflected + ignored
    return PersonaCuriosityMetricsResponse(
        window_hours=since_hours,
        plan_count=plan_count,
        ask_plan_count=ask_plan_count,
        no_ask_plan_count=no_ask_plan_count,
        asked_count=status_counts.get("asked", 0),
        answered_count=answered,
        deflected_count=deflected,
        ignored_count=ignored,
        answered_ratio=_ratio(answered, terminal_total),
        deflected_ratio=_ratio(deflected, terminal_total),
        ignored_ratio=_ratio(ignored, terminal_total),
        persona_candidate_facts_after_curiosity=candidate_count,
        repeated_question_guard_incidents=repeated_guard_incidents,
    )


@router.get(
    "/admin/observability/emotion-events",
    response_model=list[EmotionEventResponse],
)
async def list_emotion_events(
    character_id: str = Query(...),
    operator_id: str = Query(default=DEFAULT_OPERATOR_ID),
    since_hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=100, ge=1, le=500),
    container: ServiceContainer = Depends(get_container),
) -> list[EmotionEventResponse]:
    repo = container.emotion_event_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Emotion event repository is not wired",
        )
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    events = await repo.list_recent(
        character_id=character_id,
        operator_id=operator_id,
        since=since,
        limit=limit,
    )
    return [EmotionEventResponse.from_domain(e) for e in events]


# ---------- HUMANIZATION_ROADMAP P1 read-only timelines ------------------


class DispositionDriftRecordResponse(BaseModel):
    """One band-shift audit row (HUMANIZATION_ROADMAP §3.1)."""

    id: str
    character_id: str
    dimension: str
    from_band: str
    to_band: str
    reason: str
    evidence_quote: str = ""
    decided_at: datetime

    @classmethod
    def from_domain(cls, record) -> "DispositionDriftRecordResponse":
        return cls(
            id=record.id,
            character_id=record.character_id,
            dimension=record.dimension,
            from_band=record.from_band,
            to_band=record.to_band,
            reason=record.reason,
            evidence_quote=record.evidence_quote,
            decided_at=record.decided_at,
        )


@router.get(
    "/admin/observability/disposition-drift",
    response_model=list[DispositionDriftRecordResponse],
)
async def list_disposition_drift(
    character_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
    container: ServiceContainer = Depends(get_container),
) -> list[DispositionDriftRecordResponse]:
    """HUMANIZATION_ROADMAP §3.1 — 人格演化軌跡 timeline."""
    repo = container.disposition_drift_history_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Disposition drift history repository is not wired",
        )
    records = await repo.list_for_character(character_id, limit=limit)
    return [DispositionDriftRecordResponse.from_domain(r) for r in records]


class SelfReflectionResponse(BaseModel):
    """Latest reflection row (HUMANIZATION_ROADMAP §3.2)."""

    id: str
    character_id: str
    operator_id: str
    period: str
    narrative: str
    dominant_themes: list[str]
    evidence_quotes: list[str]
    period_start: str
    period_end: str
    created_at: datetime

    @classmethod
    def from_domain(cls, row) -> "SelfReflectionResponse":
        return cls(
            id=row.id,
            character_id=row.character_id,
            operator_id=row.operator_id,
            period=row.period,
            narrative=row.narrative,
            dominant_themes=list(row.dominant_themes),
            evidence_quotes=list(row.evidence_quotes),
            period_start=row.period_start.isoformat(),
            period_end=row.period_end.isoformat(),
            created_at=row.created_at,
        )


@router.get(
    "/admin/observability/self-reflections",
    response_model=list[SelfReflectionResponse],
)
async def list_self_reflections(
    character_id: str = Query(...),
    operator_id: str = Query(default=DEFAULT_OPERATOR_ID),
    container: ServiceContainer = Depends(get_container),
) -> list[SelfReflectionResponse]:
    repo = container.self_reflection_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Self reflection repository is not wired",
        )
    rows = await repo.latest_for(character_id, operator_id)
    return [SelfReflectionResponse.from_domain(r) for r in rows]


class BehavioralPatternResponse(BaseModel):
    """Pattern row (HUMANIZATION_ROADMAP §3.3)."""

    id: str
    character_id: str
    kind: str
    description: str
    observed_count: int
    salience: float
    last_observed_at: datetime

    @classmethod
    def from_domain(cls, row) -> "BehavioralPatternResponse":
        return cls(
            id=row.id,
            character_id=row.character_id,
            kind=row.kind,
            description=row.description,
            observed_count=row.observed_count,
            salience=row.salience,
            last_observed_at=row.last_observed_at,
        )


@router.get(
    "/admin/observability/behavioral-patterns",
    response_model=list[BehavioralPatternResponse],
)
async def list_behavioral_patterns(
    character_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
    container: ServiceContainer = Depends(get_container),
) -> list[BehavioralPatternResponse]:
    repo = container.behavioral_pattern_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Behavioral pattern repository is not wired",
        )
    rows = await repo.list_for_character(character_id, limit=limit)
    return [BehavioralPatternResponse.from_domain(r) for r in rows]


# ---------- Subsystem health dashboard ------------------------------------


class SubsystemHealthMetricsResponse(BaseModel):
    """Subsystem health trend signals for the admin observability panel.

    Intentionally **no target values** — these are trend curves the
    operator eyes for regressions, not SLOs. Empty inputs collapse to
    zero ratios so the dashboard renders ``-`` instead of crashing on
    new installs.
    """

    window_hours: int
    emotion_causality_ratio: float
    """0..1 fraction of recent ``EmotionEvent`` rows that have a
    ``cause_ref_id`` set (i.e. tied back to a specific turn / proactive
    / world event). Higher = more traceable inner life."""
    emotion_causality_by_kind: dict[str, int]
    """Counts of emotion events with cause_ref_id present, grouped by
    ``cause_ref_kind``. Used to spot a single subsystem leaking
    "unsourced" emotion (e.g. the proactive path forgetting to set
    ``cause_ref_id``)."""
    proactive_send_ratio: float
    """0..1 fraction of proactive attempts that ended in ``sent``.
    Combined with ``intention_skipped_ratio`` below this approximates
    the "interaction rhythm" health — too high = noisy, too low = too
    quiet."""
    proactive_intention_skipped_ratio: float
    proactive_gate_blocked_ratio: float
    emotion_followup_window_hours: int
    emotion_followup_count: int
    """How many high-intensity emotion events (intensity ≥ 0.6) in the
    window were followed within ``emotion_followup_window_hours`` by a
    chat / proactive turn whose ``post_turn_refs`` mentioned the
    event id. Pure presence signal — the closed-loop count, not a
    quality score."""
    emotion_high_intensity_total: int
    emotion_followup_ratio: float


def _ratio(part: int, total: int) -> float:
    return float(part) / float(total) if total > 0 else 0.0


@router.get(
    "/admin/observability/metrics/subsystem-health",
    response_model=SubsystemHealthMetricsResponse,
)
async def subsystem_health_metrics(
    character_id: str = Query(...),
    operator_id: str = Query(default=DEFAULT_OPERATOR_ID),
    since_hours: int = Query(default=72, ge=1, le=24 * 30),
    followup_hours: int = Query(default=24, ge=1, le=24 * 7),
    container: ServiceContainer = Depends(get_container),
) -> SubsystemHealthMetricsResponse:
    """Four-curve subsystem health dashboard.

    All four numbers come from existing observability tables. No new
    pipeline; the endpoint just slices the data we already write."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    # ---- 1. emotion causality
    emotion_repo = container.emotion_event_repository
    if emotion_repo is not None:
        events = await emotion_repo.list_recent(
            character_id=character_id,
            operator_id=operator_id,
            since=cutoff,
            limit=500,
        )
    else:
        events = []
    with_cause = [e for e in events if e.cause_ref_id]
    by_kind: dict[str, int] = {}
    for e in with_cause:
        by_kind[e.cause_ref_kind] = by_kind.get(e.cause_ref_kind, 0) + 1

    # ---- 2. proactive rhythm
    attempt_repo = container.proactive_attempt_repository
    if attempt_repo is not None:
        attempts = await attempt_repo.list_for_character(
            character_id, limit=500,
        )
    else:
        attempts = []
    recent_attempts = [a for a in attempts if a.decided_at >= cutoff]
    outcomes: dict[str, int] = {}
    for a in recent_attempts:
        outcomes[a.outcome.value] = outcomes.get(a.outcome.value, 0) + 1
    attempt_total = sum(outcomes.values())

    # ---- 3. emotion follow-up
    high_intensity = [e for e in events if e.intensity >= 0.6]
    follow_window = timedelta(hours=followup_hours)
    turn_repo = container.turn_record_repository
    referenced = 0
    if turn_repo is not None and high_intensity:
        # We want turns that fired strictly *after* the event and
        # whose post_turn_refs cite the event id. Fetch a wide window
        # once and filter in memory — turn_records is the largest table
        # so a per-event query is the wrong shape.
        turns = await turn_repo.list_recent(
            character_id=character_id,
            kind=None,
            since=cutoff - follow_window,
            limit=500,
        )
        for event in high_intensity:
            end = event.created_at + follow_window
            for turn in turns:
                if turn.created_at <= event.created_at or turn.created_at > end:
                    continue
                refs = turn.post_turn_refs or {}
                emotion_ids = refs.get("emotion_event_ids") or []
                if isinstance(emotion_ids, list) and event.id in emotion_ids:
                    referenced += 1
                    break

    return SubsystemHealthMetricsResponse(
        window_hours=since_hours,
        emotion_causality_ratio=_ratio(len(with_cause), len(events)),
        emotion_causality_by_kind=by_kind,
        proactive_send_ratio=_ratio(outcomes.get("sent", 0), attempt_total),
        proactive_intention_skipped_ratio=_ratio(
            outcomes.get("intention_skipped", 0), attempt_total,
        ),
        proactive_gate_blocked_ratio=_ratio(
            outcomes.get("gate_blocked", 0), attempt_total,
        ),
        emotion_followup_window_hours=followup_hours,
        emotion_followup_count=referenced,
        emotion_high_intensity_total=len(high_intensity),
        emotion_followup_ratio=_ratio(referenced, len(high_intensity)),
    )


# ---------- HUMANIZATION_ROADMAP §4.5 quiet hours + latency report ----------


class QuietHoursResponse(BaseModel):
    """Operator-local quiet-hours window."""

    start: int = Field(ge=0, le=23)
    end: int = Field(ge=0, le=23)


@router.get(
    "/admin/app-settings/quiet-hours",
    response_model=QuietHoursResponse,
)
async def get_quiet_hours(
    container: ServiceContainer = Depends(get_container),
) -> QuietHoursResponse:
    """HUMANIZATION_ROADMAP §4.5 — read the installation-wide default.

    Admin view: returns the global preference (or env fallback) without
    any per-user lookup. Per-user windows live behind
    ``/system/preferences/quiet-hours?scope=user`` and are honoured by
    background ticks via ``character.user_id``.
    """
    svc = container.quiet_hours_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quiet hours service is not wired",
        )
    window = await svc.window()
    return QuietHoursResponse(start=window.start, end=window.end)


class QuietHoursUpdate(BaseModel):
    start: int = Field(ge=0, le=23)
    end: int = Field(ge=0, le=23)


@router.put(
    "/admin/app-settings/quiet-hours",
    response_model=QuietHoursResponse,
)
async def put_quiet_hours(
    payload: QuietHoursUpdate,
    container: ServiceContainer = Depends(get_container),
) -> QuietHoursResponse:
    """HUMANIZATION_ROADMAP §4.5 — admin-driven global default update.

    Writes the installation-wide quiet-hours preference (user_id=None
    on the underlying ``set_window`` call). User-specific overrides
    are unaffected; only users who haven't set their own window pick
    up the change. Equivalent to
    ``PUT /system/preferences/quiet-hours?scope=global`` — kept for
    backward compatibility with the existing admin app-settings panel.
    """
    svc = container.quiet_hours_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quiet hours service is not wired",
        )
    window = await svc.set_window(
        start=payload.start, end=payload.end, user_id=None,
    )
    return QuietHoursResponse(start=window.start, end=window.end)


class HumanizationFlagsResponse(BaseModel):
    """Read-only snapshot of HumanizationSettings flags (§4.4 / §4.1 UI).

    Owner decision (2026-05-21): all flags are env-driven; this endpoint
    surfaces the resolved values so the admin UI can show operators
    which P1/P2 subsystems are currently on without forcing them to
    eyeball ``.env``. Changing a flag still requires editing the env
    file + restart — the response embeds the env var name to keep the
    UI honest about that."""

    relationship_milestone_enabled: bool
    disposition_drift_enabled: bool
    self_reflection_enabled: bool
    behavioral_pattern_enabled: bool
    deferred_intent_enabled: bool
    route_b_enabled: bool
    body_state_enabled: bool
    subjective_time_enabled: bool
    address_preference_enabled: bool
    env_prefix: str = "KOKORO_HUMANIZATION_"


class PersonaCuriosityFlagsResponse(BaseModel):
    enabled: bool
    proactive_enabled: bool
    env_names: dict[str, str] = Field(
        default_factory=lambda: {
            "enabled": "PERSONA_CURIOSITY_ENABLED",
            "proactive_enabled": "PERSONA_CURIOSITY_PROACTIVE_ENABLED",
        },
    )


@router.get(
    "/admin/app-settings/humanization-flags",
    response_model=HumanizationFlagsResponse,
)
async def get_humanization_flags(
    container: ServiceContainer = Depends(get_container),
) -> HumanizationFlagsResponse:
    settings = container.app_settings
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="App settings are not wired",
        )
    flags = settings.humanization
    return HumanizationFlagsResponse(
        relationship_milestone_enabled=flags.relationship_milestone_enabled,
        disposition_drift_enabled=flags.disposition_drift_enabled,
        self_reflection_enabled=flags.self_reflection_enabled,
        behavioral_pattern_enabled=flags.behavioral_pattern_enabled,
        deferred_intent_enabled=flags.deferred_intent_enabled,
        route_b_enabled=flags.route_b_enabled,
        body_state_enabled=flags.body_state_enabled,
        subjective_time_enabled=flags.subjective_time_enabled,
        address_preference_enabled=flags.address_preference_enabled,
    )


@router.get(
    "/admin/app-settings/persona-curiosity-flags",
    response_model=PersonaCuriosityFlagsResponse,
)
async def get_persona_curiosity_flags(
    container: ServiceContainer = Depends(get_container),
) -> PersonaCuriosityFlagsResponse:
    settings = container.app_settings
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="App settings are not wired",
        )
    flags = settings.persona
    return PersonaCuriosityFlagsResponse(
        enabled=flags.curiosity_enabled,
        proactive_enabled=flags.curiosity_proactive_enabled,
    )


class LatencyKindStats(BaseModel):
    """Per-kind latency percentile snapshot (ms)."""

    kind: str
    count: int
    p50_ms: int | None
    p90_ms: int | None
    p95_ms: int | None
    p99_ms: int | None
    max_ms: int | None


class LatencyReportResponse(BaseModel):
    window_hours: int
    overall_count: int
    per_kind: list[LatencyKindStats]


def _percentile(sorted_values: list[int], pct: float) -> int | None:
    if not sorted_values:
        return None
    idx = int(round((pct / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[idx]


@router.get(
    "/admin/observability/latency-report",
    response_model=LatencyReportResponse,
)
async def latency_report(
    since_hours: int = Query(default=24, ge=1, le=24 * 30),
    container: ServiceContainer = Depends(get_container),
) -> LatencyReportResponse:
    """HUMANIZATION_ROADMAP §4.5 — per-kind latency percentile snapshot.

    Owner decision (2026-05-21): no SLO numbers, just trend data. The
    response surfaces p50/p90/p95/p99/max per ``TurnRecord.kind`` so
    the operator can spot a regressing subsystem (proactive turn
    suddenly 4x slower) without us pinning a hard budget.
    """
    repo = container.turn_record_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Turn record repository is not wired",
        )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    records = await repo.list_recent(
        character_id=None, kind=None, since=cutoff, limit=5000,
    )
    by_kind: dict[str, list[int]] = {}
    overall = 0
    for r in records:
        if r.latency_ms is None:
            continue
        by_kind.setdefault(r.kind, []).append(int(r.latency_ms))
        overall += 1
    per_kind: list[LatencyKindStats] = []
    for kind in sorted(by_kind.keys()):
        values = sorted(by_kind[kind])
        per_kind.append(
            LatencyKindStats(
                kind=kind,
                count=len(values),
                p50_ms=_percentile(values, 50),
                p90_ms=_percentile(values, 90),
                p95_ms=_percentile(values, 95),
                p99_ms=_percentile(values, 99),
                max_ms=max(values) if values else None,
            ),
        )
    return LatencyReportResponse(
        window_hours=since_hours, overall_count=overall, per_kind=per_kind,
    )
