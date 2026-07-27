"""Admin provider writes must broadcast the change, not just apply it locally.

Before this, ``create`` / ``patch`` / ``delete`` rebuilt the registries of the
one api replica that happened to receive the HTTP request. Nothing told the
other six Hosted processes, so a disabled (e.g. leaked) API key stayed live on
them until restart. These tests pin both halves of the fix:

* the write path fires the cross-process hint after its own local sync;
* the hint itself is PostgreSQL-only — SQLite / no-database self-host emits
  nothing and stays byte-identical.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.routes import admin_providers
from kokoro_link.contracts.runtime_config_events import (
    RUNTIME_CONFIG_CHANNEL,
    RUNTIME_CONFIG_TOPIC_PROVIDERS,
)
from kokoro_link.infrastructure.persistence.runtime_config_signal import (
    notify_runtime_config_changed,
)


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-provider-secret-key")
    for key in (
        "KOKORO_OPENAI_API_KEY",
        "KOKORO_DEEPSEEK_API_KEY",
        "KOKORO_OPENROUTER_API_KEY",
        "KOKORO_GEMINI_API_KEY",
        "KOKORO_MISTRAL_API_KEY",
        "KOKORO_ANTHROPIC_API_KEY",
        "KOKORO_LMSTUDIO_MODEL",
        "KOKORO_LMSTUDIO_API_KEY",
        "KOKORO_IMAGE_API_KEY",
        "KOKORO_VIDEO_API_KEY",
        "KOKORO_TTS_API_KEY",
        "KOKORO_COMFYUI_SERVER",
        "KOKORO_TTS_BASE_URL",
        "TAVILY_API_KEY",
        "KOKORO_TAVILY_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "KOKORO_EMBEDDING_MODEL",
        "KOKORO_EMBEDDING_BASE_URL",
        "KOKORO_EMBEDDING_API_KEY",
        "YURALUME_CLOUD_ENABLED",
        "YURALUME_CLOUD_USER_SERVICE_URL",
        "YURALUME_CLOUD_GATEWAY_URL",
        "YURALUME_CLOUD_DEPLOYMENT_TOKEN",
        "YURALUME_CLOUD_DEPLOYMENT_ID",
        "YURALUME_CLOUD_DEPLOYMENT_AUDIENCE",
    ):
        monkeypatch.setenv(key, "")


def _record_notifications(monkeypatch) -> list[object]:
    sent: list[object] = []

    async def _fake_notify(engine, **kwargs):
        sent.append(engine)
        return True

    monkeypatch.setattr(
        admin_providers, "notify_runtime_config_changed", _fake_notify,
    )
    return sent


_DRAFT = {
    "provider": "openai",
    "label": "OpenAI prod",
    "enabled": True,
    "capabilities": ["llm"],
    "config": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "secret": {"api_key": "sk-unit-secret"},
}


def test_create_patch_delete_each_broadcast_the_change(monkeypatch) -> None:
    _configure_env(monkeypatch)
    sent = _record_notifications(monkeypatch)
    client = TestClient(create_app())

    created = client.post("/api/v1/admin/providers", json=_DRAFT)
    assert created.status_code == 201
    assert len(sent) == 1

    connection_id = created.json()["id"]
    patched = client.patch(
        f"/api/v1/admin/providers/{connection_id}", json={"enabled": False},
    )
    assert patched.status_code == 200
    assert len(sent) == 2

    deleted = client.delete(f"/api/v1/admin/providers/{connection_id}")
    assert deleted.status_code == 204
    assert len(sent) == 3


def test_read_only_routes_do_not_broadcast(monkeypatch) -> None:
    """Only a real mutation may wake seven processes."""
    _configure_env(monkeypatch)
    sent = _record_notifications(monkeypatch)
    client = TestClient(create_app())

    assert client.get("/api/v1/admin/providers/catalog").status_code == 200
    assert client.get("/api/v1/admin/providers").status_code == 200
    assert sent == []


# --- the signal itself ------------------------------------------------------


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeConn:
    def __init__(self, log: list[tuple[str, dict]]) -> None:
        self._log = log

    async def execute(self, statement, parameters=None):
        self._log.append((str(statement), dict(parameters or {})))
        return None


class _FakeBegin:
    def __init__(self, log: list[tuple[str, dict]]) -> None:
        self._log = log

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._log)

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeEngine:
    def __init__(self, dialect_name: str, *, explode: bool = False) -> None:
        self.dialect = _FakeDialect(dialect_name)
        self.log: list[tuple[str, dict]] = []
        self._explode = explode

    def begin(self):
        if self._explode:
            raise RuntimeError("pool exhausted")
        return _FakeBegin(self.log)


@pytest.mark.asyncio
async def test_notify_emits_pg_notify_on_postgres() -> None:
    engine = _FakeEngine("postgresql")
    assert await notify_runtime_config_changed(engine) is True
    sql, params = engine.log[0]
    assert "pg_notify" in sql
    assert params == {
        "channel": RUNTIME_CONFIG_CHANNEL,
        "payload": RUNTIME_CONFIG_TOPIC_PROVIDERS,
    }


@pytest.mark.asyncio
async def test_notify_is_a_noop_off_postgres() -> None:
    """Self-host red line: no NOTIFY, no statement, no behaviour change."""
    for dialect in ("sqlite", "mysql"):
        engine = _FakeEngine(dialect)
        assert await notify_runtime_config_changed(engine) is False
        assert engine.log == []
    assert await notify_runtime_config_changed(None) is False


@pytest.mark.asyncio
async def test_notify_failure_never_escapes_into_the_admin_write() -> None:
    engine = _FakeEngine("postgresql", explode=True)
    assert await notify_runtime_config_changed(engine) is False
