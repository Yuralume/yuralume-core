from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kokoro_link.storage_service.app import (
    DEFAULT_EPHEMERAL_TTL_SECONDS,
    MIN_EPHEMERAL_TTL_SECONDS,
    LocalStorageSettings,
    create_app,
    resolve_ephemeral_ttl_seconds,
    sweep_ephemeral_objects,
    sweep_interval_seconds,
)


def _client(tmp_path: Path, monkeypatch) -> TestClient:  # noqa: ANN001
    monkeypatch.setenv("YURALUME_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("YURALUME_STORAGE_API_KEY", "secret")
    monkeypatch.setenv("YURALUME_STORAGE_PUBLIC_BASE_URL", "http://storage.test")
    return TestClient(create_app())


def _write_stored_object(
    root: Path,
    object_key: str,
    *,
    age_seconds: float,
) -> tuple[Path, Path]:
    """Lay down an object + its metadata sidecar with a backdated mtime."""
    object_path = root / "objects" / object_key
    metadata_path = root / "metadata" / f"{object_key}.json"
    stamp = time.time() - age_seconds
    for path, payload in ((object_path, b"PNG"), (metadata_path, b"{}")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        os.utime(path, (stamp, stamp))
    return object_path, metadata_path


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


def test_ephemeral_sweep_removes_expired_draft_uploads(tmp_path: Path) -> None:
    stale_object, stale_metadata = _write_stored_object(
        tmp_path, "draft-uploads/u1/x.png", age_seconds=7200,
    )
    fresh_object, fresh_metadata = _write_stored_object(
        tmp_path, "draft-uploads/u1/fresh.png", age_seconds=10,
    )

    removed = sweep_ephemeral_objects(root=tmp_path, ttl_seconds=3600)

    assert removed == 2
    assert not stale_object.exists()
    assert not stale_metadata.exists()
    assert fresh_object.exists()
    assert fresh_metadata.exists()


def test_ephemeral_sweep_never_leaves_its_prefix(tmp_path: Path) -> None:
    kept_object, kept_metadata = _write_stored_object(
        tmp_path, "users/u1/chat-uploads/keep.png", age_seconds=86400,
    )
    also_kept, _ = _write_stored_object(
        tmp_path, "feed/char-1/old.png", age_seconds=86400,
    )

    removed = sweep_ephemeral_objects(root=tmp_path, ttl_seconds=1)

    assert removed == 0
    assert kept_object.exists()
    assert kept_metadata.exists()
    assert also_kept.exists()


def test_ephemeral_sweep_prunes_emptied_subdirectories(tmp_path: Path) -> None:
    _write_stored_object(tmp_path, "draft-uploads/u1/x.png", age_seconds=7200)

    sweep_ephemeral_objects(root=tmp_path, ttl_seconds=3600)

    assert not (tmp_path / "objects" / "draft-uploads" / "u1").exists()
    assert (tmp_path / "objects" / "draft-uploads").is_dir()
    assert not (tmp_path / "metadata" / "draft-uploads" / "u1").exists()


def test_ephemeral_sweep_is_a_noop_when_prefix_is_absent(tmp_path: Path) -> None:
    (tmp_path / "objects").mkdir()
    (tmp_path / "metadata").mkdir()

    assert sweep_ephemeral_objects(root=tmp_path, ttl_seconds=60) == 0
    assert sweep_ephemeral_objects(root=tmp_path / "missing", ttl_seconds=60) == 0


def test_ephemeral_sweep_does_not_raise_when_prefix_is_not_a_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "objects").mkdir()
    (tmp_path / "metadata").mkdir()
    (tmp_path / "objects" / "draft-uploads").write_bytes(b"not-a-dir")

    assert sweep_ephemeral_objects(root=tmp_path, ttl_seconds=60) == 0
    assert (tmp_path / "objects" / "draft-uploads").is_file()


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-number", "0", "-30"])
def test_ephemeral_ttl_falls_back_to_default(raw: str | None) -> None:
    assert resolve_ephemeral_ttl_seconds(raw) == DEFAULT_EPHEMERAL_TTL_SECONDS


def test_ephemeral_ttl_accepts_positive_override() -> None:
    assert resolve_ephemeral_ttl_seconds(" 900 ") == 900


@pytest.mark.parametrize("raw", ["30", "1", " 599 "])
def test_ephemeral_ttl_below_the_floor_is_clamped_up(raw: str) -> None:
    """A too-small TTL is the misconfiguration that *looks* like it works.

    Unparseable and non-positive values fall back loudly; a positive 30s does
    not — the sweep runs happily and every so often deletes a staged reference
    image while an upstream provider is still fetching it, surfacing as an
    intermittent failure of a charged action with nothing wrong in the logs.
    The floor is twice the cloud gateway's own 300s read timeout.
    """
    assert resolve_ephemeral_ttl_seconds(raw) == MIN_EPHEMERAL_TTL_SECONDS


def test_ephemeral_ttl_at_the_floor_is_kept_as_is() -> None:
    assert resolve_ephemeral_ttl_seconds(str(MIN_EPHEMERAL_TTL_SECONDS)) == (
        MIN_EPHEMERAL_TTL_SECONDS
    )


def test_settings_fall_back_to_default_ttl_on_unparseable_env(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("YURALUME_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("YURALUME_STORAGE_EPHEMERAL_TTL_SECONDS", "banana")

    settings = LocalStorageSettings.from_env()

    assert settings.ephemeral_ttl_seconds == DEFAULT_EPHEMERAL_TTL_SECONDS


def test_sweep_interval_is_half_the_ttl_but_floored() -> None:
    assert sweep_interval_seconds(3600) == 1800.0
    assert sweep_interval_seconds(60) == 300.0


def test_app_startup_sweeps_expired_draft_uploads(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    stale_object, stale_metadata = _write_stored_object(
        tmp_path, "draft-uploads/u1/x.png", age_seconds=7200,
    )
    fresh_object, _ = _write_stored_object(
        tmp_path, "feed/char-1/a.png", age_seconds=7200,
    )
    monkeypatch.setenv("YURALUME_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("YURALUME_STORAGE_API_KEY", "secret")
    monkeypatch.setenv("YURALUME_STORAGE_EPHEMERAL_TTL_SECONDS", "3600")

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200

    assert not stale_object.exists()
    assert not stale_metadata.exists()
    assert fresh_object.exists()
