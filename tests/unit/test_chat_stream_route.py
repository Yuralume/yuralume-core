"""End-to-end SSE regression test for the streaming chat endpoint.

The streaming handler yields ``response.model_dump()`` inside ``json.dumps``.
If the dump leaves datetime/UUID fields as Python objects, ``json.dumps``
raises ``TypeError`` mid-stream — the frontend sees tokens but never the
final ``done`` event, leaving the UI stuck on "傳送中".

This test drives the real FastAPI app with an in-memory fake provider,
asserts the stream ends with a ``done`` event containing a serialised
state, and that the state's ``last_active_at`` comes back as a string.
"""

import json

from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app


def _configure_test_app_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")


def _parse_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


def test_stream_endpoint_emits_done_event_with_serialisable_state(monkeypatch) -> None:
    # Force in-memory repositories so concurrent background post-turn /
    # memorializer tasks don't race the dev server for the shared
    # SQLite file pointed to by .env. An empty string beats load_dotenv()
    # because it uses override=False — delenv alone is not enough since
    # .env would repopulate the variable on the next from_env() call.
    _configure_test_app_env(monkeypatch)

    client = TestClient(create_app())

    create = client.post(
        "/api/v1/characters",
        json={"name": "Test", "summary": "", "personality": [], "interests": []},
    )
    assert create.status_code == 201, create.text
    character_id = create.json()["id"]

    # First turn — establishes last_active_at as a real datetime on the state.
    first = client.post(
        "/api/v1/chat/messages/stream",
        json={
            "character_id": character_id,
            "provider_id": "fake",
            "message": "hello",
        },
    )
    assert first.status_code == 200
    first_events = _parse_sse(first.text)
    assert any(e.get("done") for e in first_events)
    conv_id = next(e["conversation_id"] for e in first_events if "conversation_id" in e)

    # Second turn — now last_active_at is populated. Without mode='json'
    # this is where the backend used to crash mid-stream.
    second = client.post(
        "/api/v1/chat/messages/stream",
        json={
            "character_id": character_id,
            "conversation_id": conv_id,
            "provider_id": "fake",
            "message": "again",
        },
    )
    assert second.status_code == 200
    events = _parse_sse(second.text)
    done_events = [e for e in events if e.get("done")]
    assert len(done_events) == 1, "stream must terminate with exactly one done event"

    response = done_events[0]["response"]
    last_active = response["state"]["last_active_at"]
    assert last_active is not None
    assert isinstance(last_active, str), (
        f"last_active_at must serialise to an ISO string, got {type(last_active).__name__}"
    )
