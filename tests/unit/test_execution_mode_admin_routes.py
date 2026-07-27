"""Execution-mode admin routes (P3-B, §11 / §15).

require_admin enforcement, GET current mode/epoch, the flip 200/409/503/400
matrix, audit-on-every-attempt via caplog, and 404/405 parity when the router
is not registered (neither postgres backend nor shadow).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.dependencies import get_container, get_current_user
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.application.services.execution_mode_transition import (
    ExecutionModeTransitionService,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorCursor,
    InMemoryRuntimeOwnership,
)

pytestmark = pytest.mark.asyncio

_PG_URL = "postgresql+asyncpg://user:pass@localhost:5432/yuralume_test"
_GET = "/api/v1/admin/background-execution-mode"
_FLIP = "/api/v1/admin/background-execution-mode/flip"


class _Queue:
    async def stats(self, *, now):
        class _Stats:
            status_counts = {"claimed": 0}

        return _Stats()


class _Clock:
    def now(self):
        return datetime.now(timezone.utc)


class _StubContainer:
    def __init__(self, *, runtime_ownership=None) -> None:
        self.runtime_ownership = runtime_ownership
        if runtime_ownership is not None:
            self.execution_mode_transition = ExecutionModeTransitionService(
                ownership=runtime_ownership,
                queue=_Queue(),
                cursor=InMemoryBackgroundCoordinatorCursor(),
                clock=_Clock(),
                min_observation_interval=0.0,
            )
        else:
            self.execution_mode_transition = None


def _registered_app():
    # Registered via the postgres backend branch (direct construction bypasses
    # from_env's fail-fast, the established pattern).
    return create_app(
        AppSettings(
            database_url=_PG_URL,
            process=ProcessSettings(
                role="api", background_backend="postgres",
            ),
        ),
    )


def _client(container):
    app = _registered_app()
    app.dependency_overrides[get_container] = lambda: container
    return app, TestClient(app)


async def test_get_returns_mode_and_epoch_when_wired() -> None:
    port = InMemoryRuntimeOwnership()
    _app, client = _client(_StubContainer(runtime_ownership=port))
    resp = client.get(_GET)
    assert resp.status_code == 200
    assert resp.json() == {"mode": "embedded", "epoch": 0}


async def test_direct_flip_requires_paused_barrier_and_audits(caplog) -> None:
    port = InMemoryRuntimeOwnership()
    _app, client = _client(_StubContainer(runtime_ownership=port))
    with caplog.at_level(
        logging.INFO, logger="kokoro_link.audit.execution_mode",
    ):
        resp = client.post(
            _FLIP, json={"target_mode": "distributed", "expected_epoch": 0},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "paused_barrier_required"
    assert await port.get() == ("embedded", 0)
    audit = [
        r.getMessage() for r in caplog.records
        if r.name == "kokoro_link.audit.execution_mode"
    ]
    assert audit and "paused_barrier_required" in audit[0]


async def test_staged_flip_embedded_paused_distributed_succeeds() -> None:
    port = InMemoryRuntimeOwnership()
    _app, client = _client(_StubContainer(runtime_ownership=port))
    paused = client.post(
        _FLIP, json={"target_mode": "paused", "expected_epoch": 0},
    )
    assert paused.status_code == 200
    assert paused.json() == {"mode": "paused", "epoch": 1}
    distributed = client.post(
        _FLIP, json={"target_mode": "distributed", "expected_epoch": 1},
    )
    assert distributed.status_code == 409
    assert distributed.json()["detail"]["code"] == "drain_not_confirmed"
    observed = client.post(
        "/api/v1/admin/background-execution-mode/observe-drain",
        json={"paused_epoch": 1},
    )
    assert observed.status_code == 200
    observed = client.post(
        "/api/v1/admin/background-execution-mode/observe-drain",
        json={"paused_epoch": 1},
    )
    assert observed.status_code == 200
    assert observed.json()["confirmed"] is True
    distributed = client.post(
        _FLIP, json={"target_mode": "distributed", "expected_epoch": 1},
    )
    assert distributed.status_code == 200
    assert distributed.json() == {"mode": "distributed", "epoch": 2}
    assert await port.get() == ("distributed", 2)


async def test_flip_cas_mismatch_returns_409_and_audits(caplog) -> None:
    port = InMemoryRuntimeOwnership()
    _app, client = _client(_StubContainer(runtime_ownership=port))
    # Wrong expected_epoch (row is at epoch 0) → CAS mismatch.
    with caplog.at_level(
        logging.INFO, logger="kokoro_link.audit.execution_mode",
    ):
        resp = client.post(
            _FLIP, json={"target_mode": "distributed", "expected_epoch": 9},
        )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["code"] == "epoch_mismatch"
    assert body["epoch"] == 0  # unchanged
    assert await port.get() == ("embedded", 0)
    audit = [
        r.getMessage() for r in caplog.records
        if r.name == "kokoro_link.audit.execution_mode"
    ]
    assert audit and "epoch_mismatch" in audit[0]


async def test_flip_invalid_mode_returns_400_and_audits(caplog) -> None:
    port = InMemoryRuntimeOwnership()
    _app, client = _client(_StubContainer(runtime_ownership=port))
    with caplog.at_level(
        logging.INFO, logger="kokoro_link.audit.execution_mode",
    ):
        resp = client.post(
            _FLIP, json={"target_mode": "sideways", "expected_epoch": 0},
        )
    assert resp.status_code == 400
    audit = [
        r.getMessage() for r in caplog.records
        if r.name == "kokoro_link.audit.execution_mode"
    ]
    assert audit and "invalid_mode" in audit[0]


async def test_get_and_flip_503_when_port_unwired() -> None:
    _app, client = _client(_StubContainer(runtime_ownership=None))
    assert client.get(_GET).status_code == 503
    resp = client.post(
        _FLIP, json={"target_mode": "distributed", "expected_epoch": 0},
    )
    assert resp.status_code == 503


async def test_non_admin_forbidden() -> None:
    port = InMemoryRuntimeOwnership()
    app, client = _client(_StubContainer(runtime_ownership=port))
    app.dependency_overrides[get_current_user] = lambda: OperatorProfile(
        id="nonadmin", display_name="Not Admin", is_admin=False,
    )
    assert client.get(_GET).status_code == 403
    resp = client.post(
        _FLIP, json={"target_mode": "distributed", "expected_epoch": 0},
    )
    assert resp.status_code == 403


async def test_routes_absent_when_unregistered() -> None:
    # Neither postgres backend nor shadow → router not mounted; paths keep the
    # pre-P3-B shapes (GET 404 via SPA, POST 405), never the 200/409/503 degrade.
    app = create_app(
        AppSettings(
            database_url="",
            process=ProcessSettings(role="api", background_shadow=""),
        ),
    )
    with TestClient(app) as client:
        assert client.get(_GET).status_code == 404
        resp = client.post(
            _FLIP, json={"target_mode": "embedded", "expected_epoch": 0},
        )
        assert resp.status_code == 405
        assert resp.status_code not in (200, 409, 503)
