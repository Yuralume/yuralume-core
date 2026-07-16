from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.routes.nsfw_mode import (
    NsfwModePreferenceUpdate,
    set_nsfw_mode_preference,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile


def _configure_test_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv(
        "KOKORO_IMAGE_PROFILES",
        json.dumps([
            {
                "id": "anime_nsfw",
                "label": "Anime NSFW",
                "kind": "comfyui",
                "comfyui": {
                    "server": "127.0.0.1:8188",
                    "checkpoint": "anime.safetensors",
                },
            },
        ]),
    )


def test_nsfw_mode_preference_defaults_to_inactive(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/preferences/nsfw-mode")

    assert response.status_code == 200
    body = response.json()
    assert body["active"] is False
    assert body["configured"] is False
    assert body["locked"] is False
    assert body["ttl_seconds"] == 1800
    assert body["target"] is None


def test_nsfw_mode_preference_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    missing_target = client.put(
        "/api/v1/system/preferences/nsfw-mode",
        json={"active": True},
    )
    assert missing_target.status_code == 400

    configured = client.put(
        "/api/v1/admin/system/preferences/nsfw-mode-target",
        json={
            "llm_provider_id": "fake",
            "llm_model_id": "fake",
            "image_profile_id": "anime_nsfw",
        },
    )
    assert configured.status_code == 200
    assert configured.json() == {
        "configured": True,
        "locked": False,
        "target": {
            "llm_provider_id": "fake",
            "llm_model_id": "fake",
            "image_profile_id": "anime_nsfw",
        },
    }

    put = client.put(
        "/api/v1/system/preferences/nsfw-mode",
        json={"active": True},
    )

    assert put.status_code == 200
    body = put.json()
    assert body["active"] is True
    assert body["configured"] is True
    assert body["target"] == {
        "llm_provider_id": "fake",
        "llm_model_id": "fake",
        "image_profile_id": "anime_nsfw",
    }

    got = client.get("/api/v1/system/preferences/nsfw-mode")
    assert got.status_code == 200
    assert got.json()["active"] is True

    delete_by_put = client.put(
        "/api/v1/system/preferences/nsfw-mode",
        json={"active": False},
    )
    assert delete_by_put.status_code == 200
    assert delete_by_put.json()["active"] is False
    assert delete_by_put.json()["configured"] is True
    assert delete_by_put.json()["target"] == {
        "llm_provider_id": "fake",
        "llm_model_id": "fake",
        "image_profile_id": "anime_nsfw",
    }


def test_admin_nsfw_mode_target_rejects_unknown_targets(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.put(
        "/api/v1/admin/system/preferences/nsfw-mode-target",
        json={
            "llm_provider_id": "missing",
            "llm_model_id": "fake",
            "image_profile_id": "anime_nsfw",
        },
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_nsfw_mode_preference_is_locked_in_cloud_mode() -> None:
    container = SimpleNamespace(
        app_settings=SimpleNamespace(
            cloud=SimpleNamespace(active=True),
        ),
        nsfw_mode_service=object(),
    )

    with pytest.raises(HTTPException) as exc:
        await set_nsfw_mode_preference(
            NsfwModePreferenceUpdate(active=False),
            container=container,  # type: ignore[arg-type]
            current_user=OperatorProfile.default(),
        )

    assert exc.value.status_code == 403
