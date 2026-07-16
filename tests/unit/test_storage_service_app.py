from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kokoro_link.storage_service.app import create_app


def _client(tmp_path: Path, monkeypatch) -> TestClient:  # noqa: ANN001
    monkeypatch.setenv("YURALUME_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("YURALUME_STORAGE_API_KEY", "secret")
    monkeypatch.setenv("YURALUME_STORAGE_PUBLIC_BASE_URL", "http://storage.test")
    return TestClient(create_app())


def test_storage_service_upload_metadata_public_and_delete(
    tmp_path: Path, monkeypatch,
) -> None:
    client = _client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/v1/objects",
        headers=headers,
        data={
            "object_key": "feed/char-1/a.png",
            "content_type": "image/png",
            "metadata": '{"character_id":"char-1"}',
        },
        files={"file": ("a.png", b"PNG", "image/png")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object_key"] == "feed/char-1/a.png"
    assert payload["url"] == "http://storage.test/v1/public/feed/char-1/a.png"
    assert payload["size_bytes"] == 3

    metadata = client.get(
        "/v1/objects/metadata/feed/char-1/a.png",
        headers=headers,
    )
    assert metadata.status_code == 200
    assert metadata.json()["metadata"] == {"character_id": "char-1"}

    public = client.get("/v1/public/feed/char-1/a.png")
    assert public.status_code == 200
    assert public.content == b"PNG"
    assert public.headers["content-type"].startswith("image/png")
    assert public.headers["x-object-key"] == "feed/char-1/a.png"

    deleted = client.delete("/v1/objects/feed/char-1/a.png", headers=headers)
    assert deleted.status_code == 204
    assert client.get("/v1/public/feed/char-1/a.png").status_code == 404


def test_storage_service_requires_auth_for_protected_routes(
    tmp_path: Path, monkeypatch,
) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/objects",
        data={"object_key": "probe/a.txt", "content_type": "text/plain"},
        files={"file": ("a.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 401


def test_storage_service_accepts_compose_storage_env_aliases(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("YURALUME_STORAGE_ROOT", str(tmp_path))
    monkeypatch.delenv("YURALUME_STORAGE_API_KEY", raising=False)
    monkeypatch.delenv("YURALUME_STORAGE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("STORAGE_KEY", "compose-secret")
    monkeypatch.setenv("STORAGE_PUBLIC_URL", "http://127.0.0.1:9012")
    client = TestClient(create_app())

    response = client.post(
        "/v1/objects",
        headers={"Authorization": "Bearer compose-secret"},
        data={"object_key": "probe/a.txt", "content_type": "text/plain"},
        files={"file": ("a.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["url"] == "http://127.0.0.1:9012/v1/public/probe/a.txt"


def test_storage_service_rejects_unsafe_key(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/objects",
        headers={"Authorization": "Bearer secret"},
        data={"object_key": "../evil.txt", "content_type": "text/plain"},
        files={"file": ("evil.txt", b"bad", "text/plain")},
    )

    assert response.status_code == 400


def test_storage_service_copy(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer secret"}
    client.post(
        "/v1/objects",
        headers=headers,
        data={"object_key": "candidates/a.png", "content_type": "image/png"},
        files={"file": ("a.png", b"PNG", "image/png")},
    )

    response = client.post(
        "/v1/objects/copy",
        headers=headers,
        json={
            "source_key": "candidates/a.png",
            "destination_key": "stage/a.png",
            "metadata": {"source": "candidate"},
        },
    )

    assert response.status_code == 200
    assert response.json()["object_key"] == "stage/a.png"
    assert client.get("/v1/public/stage/a.png").content == b"PNG"
