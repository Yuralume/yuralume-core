"""Admin diagnostics routes for the shadow queue (HOSTED_CORE_SCALING §11/§12).

require_admin enforcement, the stats shape, the dead-letter listing, and a
redrive round trip whose audit line is asserted via caplog. Shadow-off degrades
stats/dead to an empty shape and redrive to 503.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.dependencies import get_container, get_current_user
from kokoro_link.application.services.background_shadow_coordinator import (
    ShadowCounters,
)
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJobSpec,
    JobStatus,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)

pytestmark = pytest.mark.asyncio

BASE_KW = {"database_url": ""}


class _FakeShadowService:
    def __init__(self, counters: ShadowCounters) -> None:
        self._counters = counters

    def snapshot(self) -> ShadowCounters:
        return self._counters


class _StubContainer:
    def __init__(self, *, queue=None, lease=None, coordinator=None, worker=None):
        self.background_job_queue = queue
        self.background_coordinator_lease = lease
        self.background_shadow_coordinator = coordinator
        self.background_shadow_worker = worker


_PG_URL = "postgresql+asyncpg://user:pass@localhost:5432/yuralume_test"


def _shadow_on_app():
    # M8: the background-jobs admin router is registered ONLY when shadow is on.
    # Build the app with shadow=postgres so the routes exist; the real container
    # is then overridden with a stub, so no DB is ever touched.
    return create_app(
        AppSettings(
            database_url=_PG_URL,
            process=ProcessSettings(role="api", background_shadow="postgres"),
        ),
    )


def _client(container) -> TestClient:
    app = _shadow_on_app()
    app.dependency_overrides[get_container] = lambda: container
    return app, TestClient(app)


async def _wired_container():
    lease = InMemoryBackgroundCoordinatorLease()
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=300, now=_now(),
    )
    queue = InMemoryBackgroundJobQueue(lease=lease)
    coordinator = _FakeShadowService(ShadowCounters(enqueued=5, deduped=1))
    worker = _FakeShadowService(ShadowCounters(claimed=4, completed=3, skipped=1))
    return (
        _StubContainer(
            queue=queue, lease=lease, coordinator=coordinator, worker=worker,
        ),
        queue,
        epoch,
    )


def _now():
    # Pre-existing d40d008 time bomb, NOT P3-Dedup: the stats route
    # (background_jobs_admin.py) computes ``lease.held = lease_until > now``
    # against the WALL clock, so a fixture pinning ``now`` to an absolute
    # instant (this file shipped ``datetime(2026, 7, 20, 12, 0, 0)`` with a
    # 300s lease TTL) reports held=False the moment wall time passes 12:05
    # UTC — the assertion started failing that afternoon. Per the spec
    # baseline directive ("never pin absolute dates against wall-clock-reading
    # routes"), read the real clock so the just-acquired lease is always live.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


async def test_stats_shape_when_wired() -> None:
    container, queue, epoch = await _wired_container()
    await queue.enqueue(BackgroundJobSpec(
        kind="character_tick", idempotency_key="character_tick:c1:0",
        due_at=_now(), fencing_epoch=epoch, character_id="c1",
    ), now=_now())
    _app, client = _client(container)

    resp = client.get("/api/v1/admin/background-jobs/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["shadow_enabled"] is True
    assert body["total"] == 1
    assert body["status_counts"] == {"queued": 1}
    assert body["coordinator"]["enqueued"] == 5
    assert body["worker"]["completed"] == 3
    assert body["lease"]["owner_id"] == "coord"
    assert body["lease"]["held"] is True


async def test_stats_degrades_when_queue_unwired() -> None:
    # Defensive branch (kept for M6): shadow router IS registered, but this
    # process's queue port is None → stats degrade to the empty shape rather
    # than 500. With M6 the queue is wired in every api-serving role, so this is
    # now a belt-and-braces path, not the common one.
    _app, client = _client(_StubContainer())
    resp = client.get("/api/v1/admin/background-jobs/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["shadow_enabled"] is False
    assert body["total"] == 0
    assert body["coordinator"] is None


async def test_admin_routes_absent_when_shadow_off() -> None:
    # M8: with shadow off the router is NOT registered, so these paths return
    # exactly the pre-Phase-2 shapes — never the shadow 200/503 degrade. Unknown
    # /api GETs 404 via the SPA handler; the POST path was never an endpoint, so
    # the SPA catch-all (GET-only) makes it a 405 — neither is a new drift.
    app = create_app(
        AppSettings(
            database_url="",
            process=ProcessSettings(role="api", background_shadow=""),
        ),
    )
    with TestClient(app) as client:
        for method, path, expected in (
            ("get", "/api/v1/admin/background-jobs/stats", 404),
            ("get", "/api/v1/admin/background-jobs/dead", 404),
            ("post", "/api/v1/admin/background-jobs/job-x/redrive", 405),
        ):
            resp = getattr(client, method)(path)
            assert resp.status_code == expected, (
                f"{method} {path} → {resp.status_code}"
            )
            # The load-bearing guarantee: never the shadow degrade shapes.
            assert resp.status_code not in (200, 503)


async def test_dead_list_and_redrive_round_trip_with_audit(caplog) -> None:
    container, queue, epoch = await _wired_container()
    job_id = await queue.enqueue(BackgroundJobSpec(
        kind="character_tick", idempotency_key="character_tick:c1:0",
        due_at=_now(), fencing_epoch=epoch, character_id="c1", max_attempts=1,
    ), now=_now())
    # Drive it to dead: claim → fail (attempt hits the cap).
    await queue.claim("w", now=_now(), limit=5, lease_seconds=60)
    await queue.fail(job_id, "w", error=RuntimeError("boom"), now=_now())

    _app, client = _client(container)

    dead = client.get("/api/v1/admin/background-jobs/dead")
    assert dead.status_code == 200
    assert [d["id"] for d in dead.json()] == [job_id]
    assert dead.json()[0]["status"] == JobStatus.DEAD.value

    with caplog.at_level(
        logging.INFO, logger="kokoro_link.audit.background_jobs",
    ):
        resp = client.post(f"/api/v1/admin/background-jobs/{job_id}/redrive")
    assert resp.status_code == 200
    assert resp.json() == {"job_id": job_id, "redriven": True}
    # The audit line records the admin identity + job id.
    audit = [
        r.getMessage() for r in caplog.records
        if r.name == "kokoro_link.audit.background_jobs"
    ]
    assert audit and job_id in audit[0] and "redrive" in audit[0]

    # Redriven job is queued again.
    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.QUEUED


async def test_redrive_unknown_job_returns_noop_and_audits(caplog) -> None:
    # An unknown (or non-dead) job id must not 404/500: the route returns 200
    # with redriven=false, and the attempt is still audited (result=noop) so
    # the trail records the try, not only the successes.
    container, _queue, _epoch = await _wired_container()
    _app, client = _client(container)

    with caplog.at_level(
        logging.INFO, logger="kokoro_link.audit.background_jobs",
    ):
        resp = client.post("/api/v1/admin/background-jobs/nope-404/redrive")
    assert resp.status_code == 200
    assert resp.json() == {"job_id": "nope-404", "redriven": False}
    audit = [
        r.getMessage() for r in caplog.records
        if r.name == "kokoro_link.audit.background_jobs"
    ]
    assert audit and "nope-404" in audit[0] and "noop" in audit[0]


async def test_redrive_503_when_queue_unwired() -> None:
    # Defensive: shadow router registered but this process has no queue port →
    # a mutation with nothing to act on is a 503 (not a silent no-op).
    _app, client = _client(_StubContainer())
    resp = client.post("/api/v1/admin/background-jobs/job-x/redrive")
    assert resp.status_code == 503


async def test_non_admin_forbidden() -> None:
    container, _queue, _epoch = await _wired_container()
    app, client = _client(container)
    app.dependency_overrides[get_current_user] = lambda: OperatorProfile(
        id="nonadmin", display_name="Not Admin", is_admin=False,
    )
    for method, path in (
        ("get", "/api/v1/admin/background-jobs/stats"),
        ("get", "/api/v1/admin/background-jobs/dead"),
        ("post", "/api/v1/admin/background-jobs/job-x/redrive"),
    ):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"
