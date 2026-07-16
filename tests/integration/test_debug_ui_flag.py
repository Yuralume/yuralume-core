"""``KOKORO_DEBUG_UI_ENABLED`` surfaces through ``GET /auth/config``.

The flag controls whether the SPA renders developer-facing admin
panels — backend admin APIs stay reachable regardless. This test
covers the wire surface so the frontend always sees the correct
default and explicit overrides.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app


def _client(monkeypatch: pytest.MonkeyPatch, *, debug_ui: str | None) -> TestClient:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    if debug_ui is None:
        monkeypatch.setenv("KOKORO_DEBUG_UI_ENABLED", "false")
    else:
        monkeypatch.setenv("KOKORO_DEBUG_UI_ENABLED", debug_ui)
    return TestClient(create_app())


def test_debug_ui_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, debug_ui=None)
    payload = client.get("/api/v1/auth/config").json()
    assert payload["debug_ui_enabled"] is False


def test_debug_ui_truthy_env_flips_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, debug_ui="true")
    payload = client.get("/api/v1/auth/config").json()
    assert payload["debug_ui_enabled"] is True


def test_debug_ui_off_string_keeps_flag_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, debug_ui="off")
    payload = client.get("/api/v1/auth/config").json()
    assert payload["debug_ui_enabled"] is False


def test_auth_config_includes_build_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YURALUME_BUILD_TAG", "v0.1.0")
    monkeypatch.setenv("YURALUME_BUILD_SHA", "abcdef123456")
    monkeypatch.setenv("YURALUME_BUILD_TIME", "2026-06-14T12:00:00Z")
    client = _client(monkeypatch, debug_ui=None)

    payload = client.get("/api/v1/auth/config").json()

    assert payload["build_info"]["name"] == "Yuralume Core"
    assert payload["build_info"]["version"]
    assert payload["build_info"]["api_version"] == "v1"
    assert payload["build_info"]["build"] == {
        "image_tag": "v0.1.0",
        "commit_sha": "abcdef123456",
        "built_at": "2026-06-14T12:00:00Z",
    }
