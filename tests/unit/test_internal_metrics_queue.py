"""Shadow queue series on the internal metrics endpoint (HOSTED_CORE_SCALING §12).

The scrape body gains the durable-queue series when the queue port is wired
(per-status job gauges, oldest-age, shadow counters, coordinator lease) and
omits them entirely when shadow is off.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.services.background_shadow_coordinator import (
    ShadowCounters,
)
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJobSpec,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)

pytestmark = pytest.mark.asyncio

_METRICS_PATH = "/api/internal/v1/metrics"
_METRICS_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_TOKEN = "scrape-secret"


def _now():
    # Pre-existing d40d008 time bomb, NOT P3-Dedup: the metrics route
    # (internal_metrics.py) computes lease ``held = lease_until > now`` against
    # the WALL clock, so a fixture pinning ``now`` to an absolute instant (this
    # file shipped ``datetime(2026, 7, 20, 12, 0, 0)`` with a 300s TTL) renders
    # held="false" once wall time passes 12:05 UTC. Per the spec baseline
    # directive ("never pin absolute dates against wall-clock-reading routes"),
    # read the real clock. See test_background_jobs_admin_routes._now.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


class _FakeShadowService:
    def __init__(self, counters: ShadowCounters) -> None:
        self._counters = counters

    def snapshot(self) -> ShadowCounters:
        return self._counters


def _app(monkeypatch):
    monkeypatch.setenv(_METRICS_ENV, _TOKEN)
    return create_app(
        AppSettings(database_url="", process=ProcessSettings(role="api")),
    )


async def _wire_queue(app):
    lease = InMemoryBackgroundCoordinatorLease()
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=300, now=_now(),
    )
    queue = InMemoryBackgroundJobQueue(lease=lease)
    await queue.enqueue(BackgroundJobSpec(
        kind="character_tick", idempotency_key="character_tick:c1:0",
        due_at=_now(), fencing_epoch=epoch, character_id="c1",
    ), now=_now())
    app.state.container.background_job_queue = queue
    app.state.container.background_coordinator_lease = lease
    app.state.container.background_shadow_coordinator = _FakeShadowService(
        ShadowCounters(enqueued=3, deduped=1),
    )
    app.state.container.background_shadow_worker = _FakeShadowService(
        ShadowCounters(claimed=2, completed=2, lease_lost=0),
    )


async def test_queue_series_present_when_wired(monkeypatch) -> None:
    app = _app(monkeypatch)
    await _wire_queue(app)
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 200
    body = resp.text
    # Scheduler series still render.
    assert "yuralume_core_scheduler_ticks_total" in body
    # Queue series appended.
    assert 'yuralume_core_background_jobs{status="queued"} 1' in body
    assert "yuralume_core_background_jobs_oldest_queued_age_seconds" in body
    assert "yuralume_core_shadow_enqueued_total 3" in body
    assert "yuralume_core_shadow_claimed_total 2" in body
    assert "yuralume_core_shadow_completed_total 2" in body
    assert 'yuralume_core_background_coordinator_lease{held="true"} 1' in body
    assert "yuralume_core_background_coordinator_lease_epoch 1" in body


async def test_queue_series_absent_when_shadow_off(monkeypatch) -> None:
    app = _app(monkeypatch)
    # No queue wired (default self-host shape).
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 200
    body = resp.text
    assert "yuralume_core_scheduler_ticks_total" in body
    assert "yuralume_core_background_jobs" not in body
    assert "yuralume_core_shadow_" not in body
