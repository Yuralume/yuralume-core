"""Execution-mode series: pure renderer + internal metrics endpoint (P3-B §12).

The renderer emits the mode info gauge, the epoch gauge and the skip counter;
the endpoint appends them only when the ownership port is wired and omits them
entirely otherwise (self-host red line).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.observability.execution_mode_metrics import (
    render_execution_mode_metrics,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryRuntimeOwnership,
)

_METRICS_PATH = "/api/internal/v1/metrics"
_METRICS_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_TOKEN = "scrape-secret"


def test_renderer_emits_mode_epoch_and_skips() -> None:
    body = render_execution_mode_metrics(
        mode="distributed", epoch=7, ownership_skips_total=4,
    )
    assert 'yuralume_core_background_execution_mode{mode="distributed"} 1' in body
    assert "yuralume_core_background_execution_mode_epoch 7" in body
    assert "yuralume_core_background_ownership_skips_total 4" in body
    assert body.endswith("\n")


def _app(monkeypatch):
    monkeypatch.setenv(_METRICS_ENV, _TOKEN)
    return create_app(
        AppSettings(database_url="", process=ProcessSettings(role="api")),
    )


@pytest.mark.asyncio
async def test_mode_series_present_when_port_wired(monkeypatch) -> None:
    app = _app(monkeypatch)
    port = InMemoryRuntimeOwnership()
    from datetime import datetime, timezone

    await port.flip("distributed", 0, now=datetime.now(timezone.utc))
    app.state.container.runtime_ownership = port
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 200
    body = resp.text
    assert "yuralume_core_scheduler_ticks_total" in body  # scheduler still there
    assert 'yuralume_core_background_execution_mode{mode="distributed"} 1' in body
    assert "yuralume_core_background_execution_mode_epoch 1" in body
    assert "yuralume_core_background_ownership_skips_total" in body


@pytest.mark.asyncio
async def test_mode_series_absent_when_port_unwired(monkeypatch) -> None:
    app = _app(monkeypatch)  # self-host default: runtime_ownership is None
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 200
    body = resp.text
    assert "yuralume_core_scheduler_ticks_total" in body
    assert "yuralume_core_background_execution_mode" not in body
    assert "yuralume_core_background_ownership_skips_total" not in body
