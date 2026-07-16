"""Connection-level ``supports_vision`` default behaviour, exercised
through the admin provider-connection routes.

Regression: a provider connection row that omits the ``supports_vision``
config key used to default to ``True`` in runtime_sync, mislabelling
text-only models (e.g. an OpenRouter deepseek route) as vision-capable →
images attached → upstream 404. The fix flips the OpenAI-compatible
default to ``False`` (key-absent means "operator never asserted vision")
while Anthropic keeps ``True`` (every catalog Claude chat model sees
images).

Kept in a NEW file because ``test_provider_settings.py`` is owned by a
separate work stream; the ``_configure_env`` monkeypatch pattern is
copied verbatim.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app


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


def test_openai_compatible_absent_vision_key_defaults_false(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openrouter",
            "label": "OpenRouter text-only",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {"default_model": "deepseek/deepseek-chat"},
            "secret": {"api_key": "sk-or-secret"},
        },
    )
    assert created.status_code == 201

    model = app.state.container.model_registry.resolve("openrouter")
    # Key absent → operator never asserted vision → default False.
    assert model.supports_vision is False


def test_openai_compatible_explicit_true_vision(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openrouter",
            "label": "OpenRouter vision",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {
                "default_model": "openai/gpt-4o",
                "supports_vision": True,
            },
            "secret": {"api_key": "sk-or-secret"},
        },
    )
    assert created.status_code == 201

    model = app.state.container.model_registry.resolve("openrouter")
    assert model.supports_vision is True


def test_openai_compatible_explicit_false_vision(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "custom_openai_compatible",
            "label": "Custom text-only",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {
                "base_url": "https://llm.example.test/v1",
                "default_model": "custom-chat",
                "supports_vision": False,
            },
            "secret": {"api_key": "sk-secret"},
        },
    )
    assert created.status_code == 201

    model = app.state.container.model_registry.resolve(
        "custom_openai_compatible",
    )
    assert model.supports_vision is False


def test_anthropic_absent_vision_key_defaults_true(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "anthropic",
            "label": "Anthropic",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {"default_model": "claude-sonnet-4-5"},
            "secret": {"api_key": "sk-ant-secret"},
        },
    )
    assert created.status_code == 201

    model = app.state.container.model_registry.resolve("anthropic")
    # Every Anthropic catalog chat model supports vision → default True.
    assert model.supports_vision is True
