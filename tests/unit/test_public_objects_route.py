from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.public_objects import router
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


def _client(storage: InMemoryObjectStorage) -> TestClient:
    app = FastAPI()
    app.state.container = SimpleNamespace(object_storage=storage)
    app.include_router(router)
    return TestClient(app)


def test_public_object_route_serves_object_storage_bytes() -> None:
    storage = InMemoryObjectStorage(public_base_url="https://kokoro.example.com")
    asyncio.run(
        storage.put_bytes(
            object_key="characters/char-1/tools/a.png",
            content=b"PNG",
            content_type="image/png",
        ),
    )
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/tools/a.png")

    assert response.status_code == 200
    assert response.content == b"PNG"
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["x-object-key"] == "characters/char-1/tools/a.png"


def test_public_object_route_supports_head() -> None:
    storage = InMemoryObjectStorage(public_base_url="https://kokoro.example.com")
    asyncio.run(
        storage.put_bytes(
            object_key="tts/char-1/a.wav",
            content=b"WAV",
            content_type="audio/wav",
        ),
    )
    client = _client(storage)

    response = client.head("/v1/public/tts/char-1/a.wav")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["content-length"] == "3"


def test_public_object_route_returns_404_for_missing_object() -> None:
    client = _client(InMemoryObjectStorage())

    response = client.get("/v1/public/missing/a.png")

    assert response.status_code == 404


def test_public_object_route_rejects_unsafe_key() -> None:
    client = _client(InMemoryObjectStorage())

    response = client.get("/v1/public/%2e%2e/secret.png")

    assert response.status_code == 400
