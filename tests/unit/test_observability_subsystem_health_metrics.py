"""Subsystem health dashboard endpoint smoke tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.dependencies import get_container
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
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
from kokoro_link.infrastructure.repositories.in_memory_turn_records import (
    InMemoryTurnRecordRepository,
)


_CHAR = "char-A"
# Anchor the fixture clock to "recently" rather than a fixed calendar
# date: the endpoint filters by ``now - since_hours``, so an absolute
# timestamp silently ages out of the query window as wall-clock time
# advances and starves every emotion/proactive assertion. Six hours ago
# keeps all derived timestamps (down to _NOW - 4h) comfortably inside
# the tests' 30-day window on any run date.
_NOW = datetime.now(timezone.utc) - timedelta(hours=6)


class _StubContainer:
    """Minimal ``ServiceContainer`` stand-in — only the metric route
    reads a handful of attributes off the container, so we don't need
    to construct the full DI graph for this endpoint test."""

    def __init__(self) -> None:
        self.emotion_event_repository = InMemoryEmotionEventRepository()
        self.proactive_attempt_repository = InMemoryProactiveAttemptRepository()
        self.turn_record_repository = InMemoryTurnRecordRepository()


def _make_container() -> tuple[_StubContainer, InMemoryEmotionEventRepository, InMemoryProactiveAttemptRepository, InMemoryTurnRecordRepository]:
    stub = _StubContainer()
    return stub, stub.emotion_event_repository, stub.proactive_attempt_repository, stub.turn_record_repository


def _build_client(container) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


@pytest.mark.asyncio
async def test_subsystem_health_metrics_zero_baseline_when_empty() -> None:
    container, _, _, _ = _make_container()
    client = _build_client(container)
    resp = client.get(
        "/api/v1/admin/observability/metrics/subsystem-health",
        params={"character_id": _CHAR, "since_hours": 24 * 30},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["emotion_causality_ratio"] == 0.0
    assert payload["proactive_send_ratio"] == 0.0
    assert payload["emotion_followup_ratio"] == 0.0
    assert payload["emotion_high_intensity_total"] == 0


@pytest.mark.asyncio
async def test_subsystem_health_metrics_reports_causality_and_rhythm() -> None:
    container, emo, attempts, _ = _make_container()
    # Three emotion events, two with cause_ref_id set.
    await emo.add(EmotionEvent.new(
        character_id=_CHAR, operator_id=DEFAULT_OPERATOR_ID,
        cause_ref_kind="turn", cause_ref_id="turn-1", now=_NOW,
    ))
    await emo.add(EmotionEvent.new(
        character_id=_CHAR, operator_id=DEFAULT_OPERATOR_ID,
        cause_ref_kind="proactive_attempt", cause_ref_id="pa-1", now=_NOW,
    ))
    await emo.add(EmotionEvent.new(
        character_id=_CHAR, operator_id=DEFAULT_OPERATOR_ID,
        cause_ref_kind="idle_drift", cause_ref_id=None, now=_NOW,
    ))
    # Three proactive attempts: 1 sent, 1 intention_skipped, 1 gate_blocked.
    for outcome, decided_at in [
        (ProactiveOutcome.SENT, _NOW),
        (ProactiveOutcome.INTENTION_SKIPPED, _NOW - timedelta(hours=1)),
        (ProactiveOutcome.GATE_BLOCKED, _NOW - timedelta(hours=2)),
    ]:
        await attempts.add(ProactiveAttempt(
            id=f"pa-{outcome.value}",
            character_id=_CHAR,
            trigger=ProactiveTrigger.TICK,
            outcome=outcome,
            reason="",
            decided_at=decided_at,
        ))

    client = _build_client(container)
    resp = client.get(
        "/api/v1/admin/observability/metrics/subsystem-health",
        params={"character_id": _CHAR, "since_hours": 24 * 30},
    )
    assert resp.status_code == 200
    payload = resp.json()
    # 2 of 3 events have cause_ref_id.
    assert payload["emotion_causality_ratio"] == pytest.approx(2 / 3)
    by_kind = payload["emotion_causality_by_kind"]
    assert by_kind["turn"] == 1
    assert by_kind["proactive_attempt"] == 1
    assert "idle_drift" not in by_kind  # only-with-cause kinds counted
    # 1/3 outcomes each.
    assert payload["proactive_send_ratio"] == pytest.approx(1 / 3)
    assert payload["proactive_intention_skipped_ratio"] == pytest.approx(1 / 3)
    assert payload["proactive_gate_blocked_ratio"] == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_subsystem_health_metrics_followup_ratio_counts_referenced_events() -> None:
    container, emo, _, turns = _make_container()
    # Two high-intensity events.
    e1 = EmotionEvent.new(
        character_id=_CHAR, operator_id=DEFAULT_OPERATOR_ID,
        cause_ref_kind="turn", cause_ref_id="turn-A",
        intensity=0.9, now=_NOW - timedelta(hours=4),
    )
    e2 = EmotionEvent.new(
        character_id=_CHAR, operator_id=DEFAULT_OPERATOR_ID,
        cause_ref_kind="turn", cause_ref_id="turn-B",
        intensity=0.7, now=_NOW - timedelta(hours=2),
    )
    await emo.add(e1)
    await emo.add(e2)
    # Only e1 has a follow-up turn citing it.
    await turns.add(TurnRecord.new(
        character_id=_CHAR, kind="chat",
        post_turn_refs={"emotion_event_ids": [e1.id]},
        now=_NOW - timedelta(hours=3),
    ))

    client = _build_client(container)
    resp = client.get(
        "/api/v1/admin/observability/metrics/subsystem-health",
        params={"character_id": _CHAR, "since_hours": 24 * 30},
    )
    payload = resp.json()
    assert payload["emotion_high_intensity_total"] == 2
    assert payload["emotion_followup_count"] == 1
    assert payload["emotion_followup_ratio"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_subsystem_health_metrics_omits_cross_channel_fixture_count() -> None:
    container, _, _, _ = _make_container()
    client = _build_client(container)
    resp = client.get(
        "/api/v1/admin/observability/metrics/subsystem-health",
        params={"character_id": _CHAR},
    )
    payload = resp.json()
    assert "cross_channel_fixture_count" not in payload
