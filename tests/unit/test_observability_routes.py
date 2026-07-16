"""Route tests for /admin/observability/* endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.observability import router as observability_router
from kokoro_link.application.services.nsfw_mode import NsfwModeService
from kokoro_link.application.services.persona_curiosity_service import (
    PersonaCuriosityService,
)
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_REST_RECOVERY,
    CAUSE_TURN,
    EmotionEvent,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.persona_curiosity import (
    PERSONA_CURIOSITY_STATUS_ASKED,
    PersonaCuriosityAttempt,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.turn_record import TurnRecord
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.repositories.in_memory_emotion_events import (
    InMemoryEmotionEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_persona_curiosity import (
    InMemoryPersonaCuriosityRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_records import (
    InMemoryTurnRecordRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    build_service_container,
)


def _client(container) -> TestClient:
    app = FastAPI()
    app.state.container = container
    app.include_router(observability_router, prefix="/api/v1")
    return TestClient(app)


def _build_container_with_observability():
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.turn_record_repository = InMemoryTurnRecordRepository()
    container.emotion_event_repository = InMemoryEmotionEventRepository()
    container.proactive_attempt_repository = InMemoryProactiveAttemptRepository()
    container.persona_curiosity_service = PersonaCuriosityService(
        repository=InMemoryPersonaCuriosityRepository(),
    )
    container.nsfw_mode_service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        ttl_seconds=600,
    )
    return harness, container


@pytest.mark.asyncio
async def test_list_turns_empty():
    _, container = _build_container_with_observability()
    client = _client(container)
    response = client.get("/api/v1/admin/observability/turns")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_turns_returns_recent_records():
    _, container = _build_container_with_observability()
    repo = container.turn_record_repository
    now = datetime.now(timezone.utc)
    for i in range(3):
        await repo.add(TurnRecord.new(
            character_id="c1",
            kind="chat",
            response_text=f"reply {i}",
            latency_ms=100 + i,
            now=now - timedelta(seconds=i),
        ))
    client = _client(container)
    response = client.get("/api/v1/admin/observability/turns?character_id=c1")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    # Sorted desc by created_at.
    assert body[0]["response_excerpt"] == "reply 0"


@pytest.mark.asyncio
async def test_get_turn_returns_full_detail():
    _, container = _build_container_with_observability()
    repo = container.turn_record_repository
    record = TurnRecord.new(
        character_id="c1",
        kind="post_turn_processor",
        prompt_pack_hash="pack-123",
        prompt_assembled="full prompt body",
        response_json={"memories": 3},
    )
    await repo.add(record)
    client = _client(container)
    response = client.get(f"/api/v1/admin/observability/turns/{record.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["prompt_assembled"] == "full prompt body"
    assert body["prompt_pack_hash"] == "pack-123"
    assert body["response_json"]["memories"] == 3
    assert body["operator_feedback"] == {}


@pytest.mark.asyncio
async def test_update_turn_operator_feedback_and_filter_list():
    _, container = _build_container_with_observability()
    repo = container.turn_record_repository
    flagged = TurnRecord.new(
        character_id="c1",
        kind="chat",
        response_text="felt off",
    )
    good = TurnRecord.new(
        character_id="c1",
        kind="chat",
        response_text="felt human",
    )
    await repo.add(flagged)
    await repo.add(good)
    client = _client(container)

    update_response = client.put(
        f"/api/v1/admin/observability/turns/{flagged.id}/operator-feedback",
        json={
            "kind": "out_of_character",
            "note": "broke role boundary",
            "tags": ["role"],
        },
    )
    client.put(
        f"/api/v1/admin/observability/turns/{good.id}/operator-feedback",
        json={"kind": "felt_human"},
    )
    list_response = client.get(
        "/api/v1/admin/observability/turns?feedback_kind=out_of_character",
    )

    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["operator_feedback"]["kind"] == "out_of_character"
    assert updated["operator_feedback"]["note"] == "broke role boundary"
    assert updated["operator_feedback"]["tags"] == ["role"]
    assert list_response.status_code == 200
    listed = list_response.json()
    assert [row["id"] for row in listed] == [flagged.id]


@pytest.mark.asyncio
async def test_update_turn_operator_feedback_rejects_unknown_kind():
    _, container = _build_container_with_observability()
    repo = container.turn_record_repository
    record = TurnRecord.new(character_id="c1", kind="chat")
    await repo.add(record)
    client = _client(container)

    response = client.put(
        f"/api/v1/admin/observability/turns/{record.id}/operator-feedback",
        json={"kind": "unclear"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_turn_missing_returns_404():
    _, container = _build_container_with_observability()
    client = _client(container)
    response = client.get("/api/v1/admin/observability/turns/missing-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_emotion_events_filters_by_character_and_window():
    _, container = _build_container_with_observability()
    repo = container.emotion_event_repository
    now = datetime.now(timezone.utc)
    fresh = EmotionEvent.new(
        character_id="c1", operator_id="op1",
        cause_ref_kind=CAUSE_TURN, emotion_label="開心",
        affection_delta=5, intensity=0.6,
        now=now - timedelta(minutes=5),
    )
    stale = EmotionEvent.new(
        character_id="c1", operator_id="op1",
        cause_ref_kind=CAUSE_REST_RECOVERY,
        fatigue_delta=-10,
        now=now - timedelta(hours=48),  # outside 24h window
    )
    other = EmotionEvent.new(
        character_id="c2", operator_id="op1",
        cause_ref_kind=CAUSE_TURN, emotion_label="忌妒",
        now=now,
    )
    await repo.add(fresh)
    await repo.add(stale)
    await repo.add(other)
    client = _client(container)
    response = client.get(
        "/api/v1/admin/observability/emotion-events"
        "?character_id=c1&operator_id=op1&since_hours=24",
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["emotion_label"] == "開心"


@pytest.mark.asyncio
async def test_emotion_events_default_operator_uses_canonical_id():
    _, container = _build_container_with_observability()
    repo = container.emotion_event_repository
    now = datetime.now(timezone.utc)
    await repo.add(EmotionEvent.new(
        character_id="c1",
        operator_id=DEFAULT_OPERATOR_ID,
        cause_ref_kind=CAUSE_TURN,
        emotion_label="安心",
        now=now,
    ))
    client = _client(container)

    response = client.get(
        "/api/v1/admin/observability/emotion-events"
        "?character_id=c1&since_hours=24",
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["operator_id"] == DEFAULT_OPERATOR_ID


@pytest.mark.asyncio
async def test_proactive_funnel_counts_intention_skipped_separately():
    _, container = _build_container_with_observability()
    repo = container.proactive_attempt_repository
    now = datetime.now(timezone.utc)
    await repo.add(ProactiveAttempt.record(
        character_id="c1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.INTENTION_SKIPPED,
        reason="not meaningful",
        now=now,
    ))
    await repo.add(ProactiveAttempt.record(
        character_id="c1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.DECIDER_SKIPPED,
        reason="decider declined",
        now=now,
    ))
    client = _client(container)

    response = client.get(
        "/api/v1/admin/observability/proactive/funnel?character_id=c1",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["intention_skipped"] == 1
    assert body["decider_skipped"] == 1


@pytest.mark.asyncio
async def test_persona_curiosity_observability_lists_attempts_and_metrics():
    _, container = _build_container_with_observability()
    service = container.persona_curiosity_service
    turn_repo = container.turn_record_repository
    now = datetime.now(timezone.utc)
    await service._repository.add(  # noqa: SLF001 - route test fixture setup
        PersonaCuriosityAttempt.new(
            character_id="c1",
            operator_id=DEFAULT_OPERATOR_ID,
            surface="chat",
            target_layer=2,
            target_topic="routine",
            question_intent="learn daily rhythm",
            status=PERSONA_CURIOSITY_STATUS_ASKED,
            created_at=now,
            metadata={"persona_candidate_fact_ids": ["fact-1"]},
        ),
    )
    await turn_repo.add(TurnRecord.new(
        character_id="c1",
        kind="chat",
        post_turn_refs={
            "persona_curiosity": {
                "surface": "chat",
                "should_ask": True,
                "target_layer": 2,
                "target_topic": "routine",
                "recent_attempt_count": 0,
            },
        },
        now=now,
    ))
    await turn_repo.add(TurnRecord.new(
        character_id="c1",
        kind="chat",
        post_turn_refs={
            "persona_curiosity": {
                "surface": "chat",
                "should_ask": False,
                "safety_reason": "recent attempt already exists",
                "recent_attempt_count": 1,
            },
        },
        now=now,
    ))
    client = _client(container)

    attempts_response = client.get(
        "/api/v1/admin/observability/persona-curiosity/attempts"
        "?character_id=c1",
    )
    metrics_response = client.get(
        "/api/v1/admin/observability/metrics/persona-curiosity"
        "?character_id=c1&since_hours=24",
    )

    assert attempts_response.status_code == 200
    attempts = attempts_response.json()
    assert attempts[0]["target_topic"] == "routine"
    assert attempts[0]["metadata"]["persona_candidate_fact_ids"] == ["fact-1"]
    assert metrics_response.status_code == 200
    metrics = metrics_response.json()
    assert metrics["plan_count"] == 2
    assert metrics["ask_plan_count"] == 1
    assert metrics["no_ask_plan_count"] == 1
    assert metrics["asked_count"] == 1
    assert metrics["persona_candidate_facts_after_curiosity"] == 1
    assert metrics["repeated_question_guard_incidents"] == 1


@pytest.mark.asyncio
async def test_latency_histogram_returns_buckets():
    _, container = _build_container_with_observability()
    repo = container.turn_record_repository
    for latency in (40, 150, 800, 4500):
        await repo.add(TurnRecord.new(
            character_id="c1", kind="chat", latency_ms=latency,
        ))
    client = _client(container)
    response = client.get(
        "/api/v1/admin/observability/turns/latency-histogram?character_id=c1",
    )
    assert response.status_code == 200
    buckets = response.json()
    # Buckets exist + total count == 4
    assert sum(b["count"] for b in buckets) == 4


@pytest.mark.asyncio
async def test_nsfw_mode_metrics_report_usage_and_turn_ratio():
    _, container = _build_container_with_observability()
    service = container.nsfw_mode_service
    repo = container.turn_record_repository
    now = datetime.now(timezone.utc)
    await service.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id="anime_nsfw",
    )
    await service.enable(user_id=DEFAULT_OPERATOR_ID)
    await service.disable(user_id=DEFAULT_OPERATOR_ID)
    await repo.add(TurnRecord.new(
        character_id="c1",
        kind="chat",
        post_turn_refs={"content_mode": "nsfw"},
        now=now,
    ))
    await repo.add(TurnRecord.new(
        character_id="c1",
        kind="chat",
        post_turn_refs={"content_mode": "normal"},
        now=now,
    ))
    await repo.add(TurnRecord.new(
        character_id="c1",
        kind="chat",
        post_turn_refs={"content_mode": "normal"},
        now=now - timedelta(hours=30),
    ))
    client = _client(container)

    response = client.get(
        "/api/v1/admin/observability/metrics/nsfw-mode?window_hours=24",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["window_hours"] == 24
    assert body["sampled_turns"] == 2
    assert body["nsfw_turns"] == 1
    assert body["normal_turns"] == 1
    assert body["nsfw_turn_ratio"] == 0.5
    assert body["current_active"] is False
    assert body["current_configured"] is True
    assert body["enable_count"] == 1
    assert body["manual_disable_count"] == 1


@pytest.mark.asyncio
async def test_emotion_events_repo_missing_returns_503():
    _, container = _build_container_with_observability()
    container.emotion_event_repository = None
    client = _client(container)
    response = client.get(
        "/api/v1/admin/observability/emotion-events?character_id=c",
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_list_turns_invalid_since_returns_422():
    _, container = _build_container_with_observability()
    client = _client(container)
    response = client.get("/api/v1/admin/observability/turns?since=not-a-date")
    assert response.status_code == 422
