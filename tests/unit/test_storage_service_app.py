from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kokoro_link.storage_service.app import (
    BACKUP_EXPORT_TTL_SECONDS,
    DEFAULT_EPHEMERAL_TTL_SECONDS,
    MIN_EPHEMERAL_TTL_SECONDS,
    LocalStorageSettings,
    _resolve_prefix_dir,
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


def test_backup_artifacts_get_their_download_window_ttl(
    tmp_path: Path,
) -> None:
    """CB2: ``character-backups/`` is ephemeral like ``draft-uploads/``
    but on the 24h download-window TTL — a two-hour-old artifact must
    survive the sweep that already removes a two-hour-old draft, while a
    past-window artifact goes."""
    fresh_backup, fresh_backup_meta = _write_stored_object(
        tmp_path, "character-backups/u1/job-a.lumebackup", age_seconds=7200,
    )
    stale_backup, stale_backup_meta = _write_stored_object(
        tmp_path,
        "character-backups/u1/job-b.lumebackup",
        age_seconds=BACKUP_EXPORT_TTL_SECONDS + 3600,
    )
    stale_draft, _ = _write_stored_object(
        tmp_path, "draft-uploads/u1/x.png", age_seconds=7200,
    )

    removed = sweep_ephemeral_objects(root=tmp_path, ttl_seconds=3600)

    assert removed == 4  # stale backup + stale draft, object + metadata each
    assert fresh_backup.exists()
    assert fresh_backup_meta.exists()
    assert not stale_backup.exists()
    assert not stale_backup_meta.exists()
    assert not stale_draft.exists()


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


def test_storage_service_purge_prefix_deletes_objects_and_metadata(
    tmp_path: Path, monkeypatch,
) -> None:
    client = _client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer secret"}
    client.post(
        "/v1/objects",
        headers=headers,
        data={"object_key": "tts/char-1/aaa.wav", "content_type": "audio/wav"},
        files={"file": ("aaa.wav", b"WAV1", "audio/wav")},
    )
    client.post(
        "/v1/objects",
        headers=headers,
        data={"object_key": "tts/char-1/bbb.wav", "content_type": "audio/wav"},
        files={"file": ("bbb.wav", b"WAV2", "audio/wav")},
    )
    client.post(
        "/v1/objects",
        headers=headers,
        data={"object_key": "tts/char-2/ccc.wav", "content_type": "audio/wav"},
        files={"file": ("ccc.wav", b"WAV3", "audio/wav")},
    )

    response = client.post(
        "/v1/objects/purge-prefix",
        headers=headers,
        json={"prefix": "tts/char-1/"},
    )

    assert response.status_code == 200
    # 2 objects removed. The 2 metadata sidecars are purged too (see the
    # untouched-sibling assertions below) but are not objects and are not
    # counted a second time — F7: "deleted" means objects removed, not
    # objects-plus-sidecars, so this adapter's count matches
    # InMemoryObjectStorage's (which has no separate sidecar tree to
    # double-count).
    assert response.json() == {"deleted": 2}
    assert client.get("/v1/public/tts/char-1/aaa.wav").status_code == 404
    assert client.get("/v1/public/tts/char-1/bbb.wav").status_code == 404
    # untouched sibling character survives.
    other = client.get("/v1/public/tts/char-2/ccc.wav")
    assert other.status_code == 200
    assert other.content == b"WAV3"


def test_storage_service_purge_prefix_prunes_emptied_subdirectories(
    tmp_path: Path, monkeypatch,
) -> None:
    client = _client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer secret"}
    client.post(
        "/v1/objects",
        headers=headers,
        data={"object_key": "tts/char-1/aaa.wav", "content_type": "audio/wav"},
        files={"file": ("aaa.wav", b"WAV1", "audio/wav")},
    )

    response = client.post(
        "/v1/objects/purge-prefix",
        headers=headers,
        json={"prefix": "tts/char-1/"},
    )

    assert response.status_code == 200
    assert not (tmp_path / "objects" / "tts" / "char-1").exists()
    assert not (tmp_path / "metadata" / "tts" / "char-1").exists()
    assert (tmp_path / "objects" / "tts").is_dir()
    assert (tmp_path / "metadata" / "tts").is_dir()


def test_storage_service_purge_prefix_of_unwritten_prefix_returns_zero(
    tmp_path: Path, monkeypatch,
) -> None:
    client = _client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/v1/objects/purge-prefix",
        headers=headers,
        json={"prefix": "tts/never-written/"},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": 0}


def test_storage_service_purge_prefix_requires_auth(
    tmp_path: Path, monkeypatch,
) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/objects/purge-prefix",
        json={"prefix": "tts/char-1/"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "prefix",
    [
        "tts/char-1",  # no trailing slash
        "tts/",  # single segment
        "",  # empty
        "/tts/char-1/",  # leading slash
        "tts/../char-1/",  # traversal segment
        "tts/./char-1/",  # "." segment
        "tts/.../",  # F5: the exact shape from the finding
        "tts/.../char-1/",  # F5: all-dots segment beyond ".."
        "tts/..../char-1/",  # F5: all-dots segment, longer run
        "tts/space bad/",  # unsafe char
    ],
)
def test_storage_service_purge_prefix_rejects_unsafe_or_shallow_prefixes(
    tmp_path: Path, monkeypatch, prefix: str,
) -> None:
    client = _client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/v1/objects/purge-prefix",
        headers=headers,
        json={"prefix": prefix},
    )

    assert response.status_code == 400


def test_resolve_prefix_dir_refuses_a_prefix_that_collapses_shallower(
    tmp_path: Path,
) -> None:
    """F5, second line of defence.

    ``validate_purge_prefix`` already refuses an all-dots segment such as
    ``"..."`` before this function is ever reached — but this test calls
    ``_resolve_prefix_dir`` directly, bypassing that validator, to prove the
    resolve-time depth check inside this function is itself sufficient. On
    Windows, ``Path.resolve()`` silently drops a trailing-dot path
    component: ``base/"tts/.../"`` resolves on disk to ``base/"tts"``, one
    segment shallower than the two segments requested. Without the guard,
    that would make ``_resolve_prefix_dir`` hand back ``tts`` itself — the
    parent of every character's TTS cache, not a scoped subtree of it.
    """
    (tmp_path / "tts").mkdir()

    assert _resolve_prefix_dir(tmp_path, "tts/.../") is None
    # the real two-segment sibling is unaffected — this isn't a blanket
    # refusal of everything under "tts/", only of the collapsing prefix.
    (tmp_path / "tts" / "char-1").mkdir()
    assert _resolve_prefix_dir(tmp_path, "tts/char-1/") == tmp_path / "tts" / "char-1"


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
