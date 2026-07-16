"""BDD: ``/system/providers`` + ``/system/providers/{id}/models`` routes.

The UI uses these two endpoints to populate the provider dropdown and
the model-under-provider dropdown. The first is a trivial list; the
second asks the resolved provider to enumerate its available models
(e.g. LM Studio's ``/v1/models``) so the operator can pick per-chat.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.dependencies import get_current_user
from kokoro_link.application.services.scoped_preferences import (
    user_preference_key,
)
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)


def _install_reasoning_effort_preflight(
    app,
    *,
    rejected: set[str] | None = None,
) -> list[tuple[str, str | None]]:
    """Give the fake provider the optional upstream-validation hook.

    Production OpenAI-compatible adapters implement this hook with a
    minimal real request. Route tests keep the network fake while still
    proving that preference writes wait for validation.
    """
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


def _configure_test_app_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")


def _non_admin_user() -> OperatorProfile:
    """Stand-in for a logged-in regular player (auth-enabled installs).

    Used via ``dependency_overrides`` so the same in-memory app that runs
    as the implied-admin default operator can be probed as a non-admin —
    the auth-disabled harness has no other way to express a non-admin
    caller (the default operator is always admin)."""
    return OperatorProfile(id="player-1", display_name="Player", is_admin=False)


def test_list_providers_returns_at_least_fake(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/providers")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert "fake" in body


def test_list_models_for_fake_provider(monkeypatch) -> None:
    """Fake provider is single-model — it surfaces one entry so the UI
    doesn't render an empty dropdown."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/providers/fake/models")

    assert response.status_code == 200
    body = response.json()
    assert body == ["fake"]


def test_list_models_for_unknown_provider_returns_404(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/providers/does-not-exist/models")

    assert response.status_code == 404


# ---- active-model preference ----------------------------------------


def test_active_model_preference_defaults_to_nulls(monkeypatch) -> None:
    """Before the user picks anything, GET returns a pair of nulls —
    not 404, not the server default. The frontend treats this as
    "nothing saved, use first-available provider" without a special
    code path for missing state."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/preferences/active-model")

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": None, "model_id": None, "supports_vision": None,
    }


def test_active_model_preference_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/active-model",
        json={"provider_id": "fake", "model_id": "fake"},
    )
    assert put.status_code == 200

    got = client.get("/api/v1/system/preferences/active-model")
    assert got.status_code == 200
    assert got.json() == {
        "provider_id": "fake", "model_id": "fake", "supports_vision": None,
    }


def test_active_model_preference_accepts_null_model(monkeypatch) -> None:
    """``model_id = null`` is the "use provider default" signal —
    must round-trip intact without becoming an empty string."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/active-model",
        json={"provider_id": "lmstudio", "model_id": None},
    )
    assert put.status_code == 200
    assert put.json() == {
        "provider_id": "lmstudio", "model_id": None, "supports_vision": None,
    }

    got = client.get("/api/v1/system/preferences/active-model")
    assert got.json() == {
        "provider_id": "lmstudio", "model_id": None, "supports_vision": None,
    }


# ---- feature-model preference ----------------------------------------


def test_feature_model_preferences_include_and_persist_chat(monkeypatch) -> None:
    """Admin global LLM routing owns the chat feature explicitly.

    ``active_model`` is only the fallback for unpinned features; the
    player-facing chat model must be visible and writable in Admin.
    """
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    initial = client.get("/api/v1/system/preferences/feature-models")
    assert initial.status_code == 200
    assert "chat" in initial.json()["known_keys"]
    assert "image_recognition" in initial.json()["known_keys"]
    assert initial.json()["labels"]["chat"] == "聊天主回覆"
    assert initial.json()["labels"]["image_recognition"] == "圖片識別前處理"

    put = client.put(
        "/api/v1/system/preferences/feature-models",
        json={
            "overrides": {
                "chat": {"provider_id": "fake", "model_id": "fake"},
            },
        },
    )
    assert put.status_code == 200
    assert put.json()["overrides"]["chat"] == {
        "provider_id": "fake",
        "model_id": "fake",
        "reasoning": None,
        "supports_vision": None,
    }

    got = client.get("/api/v1/system/preferences/feature-models")
    assert got.status_code == 200
    assert got.json()["overrides"]["chat"] == {
        "provider_id": "fake",
        "model_id": "fake",
        "reasoning": None,
        "supports_vision": None,
    }


def test_feature_model_group_preferences_return_backend_catalogue(
    monkeypatch,
) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/preferences/feature-model-groups")

    assert response.status_code == 200
    body = response.json()
    group_keys = {group["key"] for group in body["groups"]}
    assert "player_facing_voice" in group_keys
    assert "multimodal_perception" in group_keys
    assert "core_structured_memory" in group_keys
    player_group = next(
        group for group in body["groups"]
        if group["key"] == "player_facing_voice"
    )
    assert player_group["label"]
    assert player_group["description"]
    assert player_group["model_guidance"]
    assert {"key": "chat", "label": "聊天主回覆"} in player_group["members"]
    assert player_group["model"] is None
    multimodal_group = next(
        group for group in body["groups"]
        if group["key"] == "multimodal_perception"
    )
    assert {
        "key": "image_recognition",
        "label": "圖片識別前處理",
    } in multimodal_group["members"]
    assert body["active_model"] == {
        "provider_id": None, "model_id": None, "supports_vision": None,
    }


def test_feature_model_group_preferences_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/feature-model-groups",
        json={
            "feature_model_groups": {
                "player_facing_voice": {
                    "provider_id": "fake",
                    "model_id": "fake",
                },
            },
        },
    )

    assert put.status_code == 200
    player_group = next(
        group for group in put.json()["groups"]
        if group["key"] == "player_facing_voice"
    )
    assert player_group["model"] == {
        "provider_id": "fake",
        "model_id": "fake",
        "reasoning": None,
        "supports_vision": None,
    }

    got = client.get("/api/v1/system/preferences/feature-model-groups")
    player_group = next(
        group for group in got.json()["groups"]
        if group["key"] == "player_facing_voice"
    )
    assert player_group["model"] == {
        "provider_id": "fake",
        "model_id": "fake",
        "reasoning": None,
        "supports_vision": None,
    }


def test_feature_model_group_preferences_reject_unknown_group(
    monkeypatch,
) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.put(
        "/api/v1/system/preferences/feature-model-groups",
        json={
            "feature_model_groups": {
                "typo_group": {"provider_id": "fake", "model_id": "fake"},
            },
        },
    )

    assert response.status_code == 400


# ---- routing-level reasoning overrides --------------------------------


def test_feature_model_reasoning_override_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    app = create_app()
    calls = _install_reasoning_effort_preflight(app)
    client = TestClient(app)

    put = client.put(
        "/api/v1/system/preferences/feature-models",
        json={
            "overrides": {
                "chat": {
                    "provider_id": "fake",
                    "model_id": "fake",
                    "reasoning": {"reasoning_effort": "high"},
                },
            },
        },
    )
    assert put.status_code == 200
    assert put.json()["overrides"]["chat"]["reasoning"] == {
        "disable_reasoning": False,
        "reasoning_effort": "high",
        "thinking_budget_tokens": None,
    }

    got = client.get("/api/v1/system/preferences/feature-models")
    assert got.json()["overrides"]["chat"]["reasoning"] == {
        "disable_reasoning": False,
        "reasoning_effort": "high",
        "thinking_budget_tokens": None,
    }
    assert calls == [("high", "fake")]


def test_group_reasoning_only_entry_persists_without_model_pin(
    monkeypatch,
) -> None:
    """The "same model, different effort per group" configuration: an
    entry that pins NO provider/model but carries reasoning must survive
    the write-side blank-entry filter."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    calls = _install_reasoning_effort_preflight(app)
    client = TestClient(app)

    active = client.put(
        "/api/v1/system/preferences/active-model",
        json={"provider_id": "fake", "model_id": "fake"},
    )
    assert active.status_code == 200

    put = client.put(
        "/api/v1/system/preferences/feature-model-groups",
        json={
            "feature_model_groups": {
                "high_reasoning_gates": {
                    "reasoning": {"reasoning_effort": "high"},
                },
                "light_observers": {
                    "reasoning": {"disable_reasoning": True},
                },
            },
        },
    )
    assert put.status_code == 200

    got = client.get("/api/v1/system/preferences/feature-model-groups")
    by_key = {group["key"]: group for group in got.json()["groups"]}
    assert by_key["high_reasoning_gates"]["model"] == {
        "provider_id": None,
        "model_id": None,
        "reasoning": {
            "disable_reasoning": False,
            "reasoning_effort": "high",
            "thinking_budget_tokens": None,
        },
        "supports_vision": None,
    }
    assert by_key["light_observers"]["model"]["reasoning"] == {
        "disable_reasoning": True,
        "reasoning_effort": None,
        "thinking_budget_tokens": None,
    }
    assert calls == [("high", "fake")]


def test_group_reasoning_effort_is_rejected_before_preference_save(
    monkeypatch,
) -> None:
    _configure_test_app_env(monkeypatch)
    app = create_app()
    calls = _install_reasoning_effort_preflight(app, rejected={"max"})
    client = TestClient(app)

    response = client.put(
        "/api/v1/system/preferences/feature-model-groups",
        json={
            "feature_model_groups": {
                "high_reasoning_gates": {
                    "provider_id": "fake",
                    "model_id": "gpt-5.6-luna",
                    "reasoning": {"reasoning_effort": "max"},
                },
            },
        },
    )

    assert response.status_code == 422
    assert "unsupported reasoning effort" in response.json()["detail"]
    assert calls == [("max", "gpt-5.6-luna")]
    got = client.get("/api/v1/system/preferences/feature-model-groups")
    by_key = {group["key"]: group for group in got.json()["groups"]}
    assert by_key["high_reasoning_gates"]["model"] is None


def test_feature_reasoning_effort_preflight_inherits_group_target(
    monkeypatch,
) -> None:
    _configure_test_app_env(monkeypatch)
    app = create_app()
    calls = _install_reasoning_effort_preflight(app)
    client = TestClient(app)

    group = client.put(
        "/api/v1/system/preferences/feature-model-groups",
        json={
            "feature_model_groups": {
                "high_reasoning_gates": {
                    "provider_id": "fake",
                    "model_id": "group-model",
                },
            },
        },
    )
    assert group.status_code == 200

    feature = client.put(
        "/api/v1/system/preferences/feature-models",
        json={
            "overrides": {
                "scene_access": {
                    "reasoning": {"reasoning_effort": "xhigh"},
                },
            },
        },
    )

    assert feature.status_code == 200
    assert calls == [("xhigh", "group-model")]


def test_blank_reasoning_object_treated_as_absent(monkeypatch) -> None:
    """An all-default reasoning object is indistinguishable from "not
    set" — the entry collapses to blank and is dropped like before."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/feature-models",
        json={
            "overrides": {
                "chat": {
                    "reasoning": {
                        "disable_reasoning": False,
                        "reasoning_effort": "   ",
                    },
                },
            },
        },
    )
    assert put.status_code == 200
    assert put.json()["overrides"] == {}


def test_reasoning_budget_rejects_non_positive(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/feature-model-groups",
        json={
            "feature_model_groups": {
                "high_reasoning_gates": {
                    "reasoning": {"thinking_budget_tokens": 0},
                },
            },
        },
    )
    assert put.status_code == 422


# ---- routing-level vision overrides -----------------------------------


def test_feature_model_vision_override_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/feature-models",
        json={
            "overrides": {
                "chat": {
                    "provider_id": "fake",
                    "model_id": "fake",
                    "supports_vision": False,
                },
            },
        },
    )
    assert put.status_code == 200
    assert put.json()["overrides"]["chat"]["supports_vision"] is False

    got = client.get("/api/v1/system/preferences/feature-models")
    assert got.json()["overrides"]["chat"]["supports_vision"] is False


def test_active_model_vision_override_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/active-model",
        json={
            "provider_id": "fake",
            "model_id": "fake",
            "supports_vision": True,
        },
    )
    assert put.status_code == 200
    assert put.json()["supports_vision"] is True

    got = client.get("/api/v1/system/preferences/active-model")
    assert got.json()["supports_vision"] is True


def test_group_vision_only_entry_persists_without_model_pin(monkeypatch) -> None:
    """An entry that pins ONLY supports_vision (no provider/model) must
    survive the write-side blank-entry filter."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/feature-model-groups",
        json={
            "feature_model_groups": {
                "multimodal_perception": {"supports_vision": True},
            },
        },
    )
    assert put.status_code == 200

    got = client.get("/api/v1/system/preferences/feature-model-groups")
    by_key = {group["key"]: group for group in got.json()["groups"]}
    assert by_key["multimodal_perception"]["model"] == {
        "provider_id": None,
        "model_id": None,
        "reasoning": None,
        "supports_vision": True,
    }


def test_blank_entry_still_dropped_with_null_vision(monkeypatch) -> None:
    """A vision-null, provider/model-blank entry is still "nothing pinned"
    and collapses away — same as before this feature."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/feature-models",
        json={
            "overrides": {
                "chat": {"supports_vision": None},
            },
        },
    )
    assert put.status_code == 200
    assert put.json()["overrides"] == {}


def test_non_bool_vision_value_rejected(monkeypatch) -> None:
    """A non-coercible non-bool supports_vision is rejected by pydantic
    with 422 (coercible strings like "true" would be accepted; a bare
    word like "banana" cannot be coerced to bool)."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/feature-models",
        json={
            "overrides": {
                "chat": {"supports_vision": "banana"},
            },
        },
    )
    assert put.status_code == 422


# ---- TTS pregeneration preference ------------------------------------


def test_tts_pregeneration_preference_defaults_to_disabled(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/preferences/tts-pregeneration")

    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_tts_pregeneration_preference_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/tts-pregeneration",
        json={"enabled": True},
    )
    assert put.status_code == 200
    assert put.json() == {"enabled": True}

    got = client.get("/api/v1/system/preferences/tts-pregeneration")
    assert got.status_code == 200
    assert got.json() == {"enabled": True}


# ---- chat assist preference -----------------------------------------


def test_chat_assist_preference_defaults_to_enabled(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/preferences/chat-assist")

    assert response.status_code == 200
    assert response.json() == {"enabled": True}


def test_chat_assist_preference_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/chat-assist",
        json={"enabled": False},
    )
    assert put.status_code == 200
    assert put.json() == {"enabled": False}

    got = client.get("/api/v1/system/preferences/chat-assist")
    assert got.status_code == 200
    assert got.json() == {"enabled": False}


# ---- visual generation style preference -----------------------------


def test_visual_generation_style_defaults_to_anime(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/preferences/visual-generation-style")

    assert response.status_code == 200
    assert response.json() == {"style": "anime"}


def test_visual_generation_style_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/visual-generation-style",
        json={"style": "realistic"},
    )
    assert put.status_code == 200
    assert put.json() == {"style": "realistic"}

    got = client.get("/api/v1/system/preferences/visual-generation-style")
    assert got.status_code == 200
    assert got.json() == {"style": "realistic"}


def test_visual_generation_style_rejects_unknown_value(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.put(
        "/api/v1/system/preferences/visual-generation-style",
        json={"style": "oil-painting"},
    )

    assert response.status_code == 400


# ---- admin gate on routing-preference writes -------------------------
#
# Post-immersion product decision: LLM / image / video ROUTING is
# admin-only. The WRITE (PUT) handlers must reject non-admin callers
# regardless of ``scope`` (default and ``scope=user`` both), because the
# resolver lets a user-scoped row shadow the global one — an unshielded
# user-scope write is a live routing bypass. GET stays open (read-only
# surfaces still need it). Player-owned prefs (chat-assist,
# tts-pregeneration, visual-generation-style) stay player-writable.


_ROUTING_PUT_CASES = [
    (
        "/api/v1/system/preferences/active-model",
        {"provider_id": "fake", "model_id": "fake"},
    ),
    (
        "/api/v1/system/preferences/feature-models",
        {"overrides": {"chat": {"provider_id": "fake", "model_id": "fake"}}},
    ),
    (
        "/api/v1/system/preferences/feature-model-groups",
        {
            "feature_model_groups": {
                "player_facing_voice": {
                    "provider_id": "fake",
                    "model_id": "fake",
                },
            },
        },
    ),
    (
        "/api/v1/system/preferences/active-image-profile",
        {"profile_id": None},
    ),
    (
        "/api/v1/system/preferences/image-feature-profiles",
        {"overrides": {}},
    ),
    (
        "/api/v1/system/preferences/active-video-profile",
        {"profile_id": None},
    ),
    (
        "/api/v1/system/preferences/video-feature-profiles",
        {"overrides": {}},
    ),
]


_ROUTING_GET_PATHS = [path for path, _ in _ROUTING_PUT_CASES]


@pytest.mark.parametrize(("path", "body"), _ROUTING_PUT_CASES)
@pytest.mark.parametrize("suffix", ["", "?scope=user", "?scope=global"])
def test_routing_preference_put_requires_admin(
    monkeypatch, path, body, suffix,
) -> None:
    """Non-admin PUT is 403 on the default scope and on both explicit
    scopes — the gate fires before scope resolution so it can't be
    dodged."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_current_user] = _non_admin_user
    try:
        client = TestClient(app)
        response = client.put(f"{path}{suffix}", json=body)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


@pytest.mark.parametrize(("path", "body"), _ROUTING_PUT_CASES)
def test_routing_preference_put_allowed_for_admin(monkeypatch, path, body) -> None:
    """The implied-admin default operator keeps write access — admin
    routing surfaces stay functional."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.put(path, json=body)

    assert response.status_code == 200


@pytest.mark.parametrize("path", _ROUTING_GET_PATHS)
def test_routing_preference_get_allowed_for_non_admin(monkeypatch, path) -> None:
    """GET stays open to regular players (read-only surfaces depend on
    it); only the write path is gated."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_current_user] = _non_admin_user
    try:
        client = TestClient(app)
        response = client.get(path)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


_PLAYER_PUT_CASES = [
    ("/api/v1/system/preferences/chat-assist", {"enabled": False}),
    ("/api/v1/system/preferences/scene-access-hint", {"enabled": False}),
    ("/api/v1/system/preferences/tts-pregeneration", {"enabled": True}),
    (
        "/api/v1/system/preferences/visual-generation-style",
        {"style": "realistic"},
    ),
]


@pytest.mark.parametrize(("path", "body"), _PLAYER_PUT_CASES)
def test_player_preference_put_allowed_for_non_admin(
    monkeypatch, path, body,
) -> None:
    """Player-owned prefs are NOT routing and must stay player-writable
    even under the admin gate on the routing writes."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_current_user] = _non_admin_user
    try:
        client = TestClient(app)
        response = client.put(path, json=body)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


# ---- default scope on routing endpoints is GLOBAL ---------------------
#
# Routing preferences are deployment-level config and their PUTs are
# admin-only, so a scope-less call (curl / admin console) must land on
# the GLOBAL row. The old ``user`` default silently wrote the admin's
# own ``u:<hash>:`` shadow row, which the resolver then preferred over
# global for that admin alone — "I changed the config but it only
# applies to me" (the 2026-06-12 legacy-row incident class). GETs
# default to global too so a scope-less read shows exactly what a
# scope-less write persisted. Explicit ``?scope=user`` keeps working.
# Player-owned prefs keep the ``user`` default — personal settings,
# not routing.


_PREF_KEY_BY_PATH = {
    "/api/v1/system/preferences/active-model": "active_model",
    "/api/v1/system/preferences/feature-models": "feature_models",
    "/api/v1/system/preferences/feature-model-groups": "feature_model_groups",
    "/api/v1/system/preferences/active-image-profile": "active_image_profile",
    "/api/v1/system/preferences/image-feature-profiles": (
        "image_feature_profiles"
    ),
    "/api/v1/system/preferences/active-video-profile": "active_video_profile",
    "/api/v1/system/preferences/video-feature-profiles": (
        "video_feature_profiles"
    ),
}


@pytest.mark.parametrize(("path", "body"), _ROUTING_PUT_CASES)
def test_routing_put_without_scope_writes_global_row(
    monkeypatch, path, body,
) -> None:
    """A scope-less admin PUT persists to the GLOBAL row — never to the
    caller's own user-scoped shadow row."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    client = TestClient(app)
    repo = app.state.container.preferences_repository
    key = _PREF_KEY_BY_PATH[path]
    assert asyncio.run(repo.get(key)) is None

    response = client.put(path, json=body)

    assert response.status_code == 200
    assert asyncio.run(repo.get(key)) is not None
    shadow = asyncio.run(
        repo.get(user_preference_key(DEFAULT_OPERATOR_ID, key)),
    )
    assert shadow is None


def test_routing_get_without_scope_reads_global_not_user_shadow(
    monkeypatch,
) -> None:
    """A scope-less GET returns the GLOBAL row even when a legacy
    user-scoped shadow row exists for the caller — what an admin reads
    without ``?scope`` is what their scope-less PUT wrote."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    client = TestClient(app)
    repo = app.state.container.preferences_repository

    async def seed() -> None:
        await repo.set(
            "active_model",
            {"provider_id": "global-provider", "model_id": "global-model"},
        )
        await repo.set(
            user_preference_key(DEFAULT_OPERATOR_ID, "active_model"),
            {"provider_id": "shadow-provider", "model_id": "shadow-model"},
        )

    asyncio.run(seed())

    got = client.get("/api/v1/system/preferences/active-model")

    assert got.status_code == 200
    assert got.json()["provider_id"] == "global-provider"

    # The shadow row stays reachable through an explicit user scope.
    scoped = client.get("/api/v1/system/preferences/active-model?scope=user")
    assert scoped.json()["provider_id"] == "shadow-provider"


@pytest.mark.parametrize("path", _ROUTING_GET_PATHS)
def test_routing_get_global_scope_open_to_non_admin(
    monkeypatch, path,
) -> None:
    """Explicit ``?scope=global`` READ is open to players: fresh
    accounts already see the global values through the user fallback,
    so the explicit read leaks nothing new — and with ``global`` as the
    scope default, closing it would 403 every scope-less player read.
    Writes stay admin-gated."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_current_user] = _non_admin_user
    try:
        client = TestClient(app)
        response = client.get(f"{path}?scope=global")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/system/preferences/active-model",
        "/api/v1/system/preferences/chat-assist",
        "/api/v1/system/preferences/scene-access-hint",
    ],
)
def test_preference_scope_rejects_unknown_value(monkeypatch, path) -> None:
    """``scope`` only accepts ``user`` / ``global`` — on the routing
    endpoints and the player ones alike."""
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get(f"{path}?scope=banana")

    assert response.status_code == 422


def test_player_preference_put_without_scope_stays_user_scoped(
    monkeypatch,
) -> None:
    """Flipping the routing default must not leak into player prefs: a
    scope-less player write still lands on the player's own row and
    leaves the global row untouched."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_current_user] = _non_admin_user
    try:
        client = TestClient(app)
        response = client.put(
            "/api/v1/system/preferences/chat-assist",
            json={"enabled": False},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    repo = app.state.container.preferences_repository
    assert asyncio.run(repo.get("chat_assist")) is None
    assert asyncio.run(
        repo.get(user_preference_key("player-1", "chat_assist")),
    ) is not None
