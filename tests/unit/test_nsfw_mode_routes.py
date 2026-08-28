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
            "reasoning": None,
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
        "reasoning": None,
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
        "reasoning": None,
    }


def test_admin_nsfw_mode_target_roundtrips_disabled_image(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    configured = client.put(
        "/api/v1/admin/system/preferences/nsfw-mode-target",
        json={
            "llm_provider_id": "fake",
            "llm_model_id": "fake",
            "image_profile_id": None,
        },
    )
    assert configured.status_code == 200
    assert configured.json() == {
        "configured": True,
        "locked": False,
        "target": {
            "llm_provider_id": "fake",
            "llm_model_id": "fake",
            "image_profile_id": None,
            "reasoning": None,
        },
    }

    got = client.get("/api/v1/admin/system/preferences/nsfw-mode-target")
    assert got.status_code == 200
    assert got.json()["target"]["image_profile_id"] is None

    # The mode stays enableable with image generation explicitly off.
    put = client.put(
        "/api/v1/system/preferences/nsfw-mode",
        json={"active": True},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["active"] is True
    assert body["target"] == {
        "llm_provider_id": "fake",
        "llm_model_id": "fake",
        "image_profile_id": None,
        "reasoning": None,
    }

    # Explicit off is null, not empty string — "" stays rejected so no
    # fake profile id can flow into registry lookups.
    rejected = client.put(
        "/api/v1/admin/system/preferences/nsfw-mode-target",
        json={
            "llm_provider_id": "fake",
            "llm_model_id": "fake",
            "image_profile_id": "",
        },
    )
    assert rejected.status_code == 422


def _install_reasoning_effort_preflight(
    app,
    *,
    rejected: set[str] | None = None,
) -> list[tuple[str, str | None]]:
    """Give the fake provider the optional upstream-validation hook —
    same harness as the feature/group routing tests."""
    calls: list[tuple[str, str | None]] = []
    rejected_values = rejected or set()
    model = app.state.container.model_registry.resolve("fake")

    async def validate_reasoning_effort(
        effort: str,
        *,
        model: str | None = None,
    ) -> None:
        calls.append((effort, model))
        if effort in rejected_values:
            raise ValueError(f"unsupported reasoning effort: {effort}")

    setattr(model, "validate_reasoning_effort", validate_reasoning_effort)
    return calls


def test_admin_nsfw_mode_target_reasoning_roundtrip(monkeypatch) -> None:
    """The NSFW target carries its own optional reasoning posture —
    persisted, normalised (all-default collapses to null) and echoed on
    both admin GET and the player-facing status target."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    calls = _install_reasoning_effort_preflight(app)
    client = TestClient(app)

    configured = client.put(
        "/api/v1/admin/system/preferences/nsfw-mode-target",
        json={
            "llm_provider_id": "fake",
            "llm_model_id": "fake",
            "image_profile_id": None,
            "reasoning": {"reasoning_effort": "high"},
        },
    )
    assert configured.status_code == 200
    assert configured.json()["target"]["reasoning"] == {
        "disable_reasoning": False,
        "reasoning_effort": "high",
        "thinking_budget_tokens": None,
    }
    assert calls == [("high", "fake")]

    got = client.get("/api/v1/admin/system/preferences/nsfw-mode-target")
    assert got.json()["target"]["reasoning"] == {
        "disable_reasoning": False,
        "reasoning_effort": "high",
        "thinking_budget_tokens": None,
    }

    # All-default posture collapses to null on write.
    cleared = client.put(
        "/api/v1/admin/system/preferences/nsfw-mode-target",
        json={
            "llm_provider_id": "fake",
            "llm_model_id": "fake",
            "image_profile_id": None,
            "reasoning": {
                "disable_reasoning": False,
                "reasoning_effort": None,
                "thinking_budget_tokens": None,
            },
        },
    )
    assert cleared.status_code == 200
    assert cleared.json()["target"]["reasoning"] is None


def test_admin_nsfw_mode_target_effort_rejected_before_save(
    monkeypatch,
) -> None:
    _configure_test_app_env(monkeypatch)
    app = create_app()
    calls = _install_reasoning_effort_preflight(app, rejected={"max"})
    client = TestClient(app)

    response = client.put(
        "/api/v1/admin/system/preferences/nsfw-mode-target",
        json={
            "llm_provider_id": "fake",
            "llm_model_id": "fake",
            "image_profile_id": None,
            "reasoning": {"reasoning_effort": "max"},
        },
    )

    assert response.status_code == 422
    assert "unsupported reasoning effort" in response.json()["detail"]
    assert calls == [("max", "fake")]
    got = client.get("/api/v1/admin/system/preferences/nsfw-mode-target")
    assert got.json()["configured"] is False


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
