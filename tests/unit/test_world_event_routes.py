import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.routes.world_events import trigger_ingest
from kokoro_link.application.services.rss_ingestion_service import IngestionReport


def test_list_world_event_sources_exposes_health_status(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/admin/world-events/sources")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert body["enabled"] >= 1
    assert body["failing"] == 0

    ncdr = next(
        source for source in body["sources"]
        if source["id"] == "ncdr-all-alerts"
    )
    assert ncdr["category"] == "emergency"
    assert ncdr["health_status"] == "unknown"
    assert ncdr["last_error"] is None


@pytest.mark.asyncio
async def test_trigger_ingest_exposes_additive_failure_counters() -> None:
    class _Scheduler:
        async def trigger_ingest_now(self) -> IngestionReport:
            return IngestionReport(
                sources_attempted=2,
                sources_succeeded=1,
                events_persisted=3,
                events_skipped_dedup=4,
                events_skipped_embed=5,
                errors=("feed: http_status: HTTP 410",),
                events_failed_persist=6,
                embed_batches_failed=7,
            )

    class _Container:
        world_event_scheduler = _Scheduler()

    body = await trigger_ingest(_Container())  # type: ignore[arg-type]

    assert body["events_failed_persist"] == 6
    assert body["embed_batches_failed"] == 7
