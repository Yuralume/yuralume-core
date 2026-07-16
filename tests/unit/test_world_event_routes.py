from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app


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
