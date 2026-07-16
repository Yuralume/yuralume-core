from pathlib import Path

import pytest

from kokoro_link.bootstrap.settings import AppSettings


def test_app_settings_loads_values_from_dotenv(tmp_path: Path, monkeypatch) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            [
                "KOKORO_LMSTUDIO_MODEL=gemma-4-31b-it-uncensored",
                "KOKORO_DEFAULT_PROVIDER_ID=lmstudio",
                "KOKORO_LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1",
                "KOKORO_LMSTUDIO_API_KEY=lm-studio",
                "KOKORO_DEPLOYMENT_MODE=container",
                "KOKORO_STORAGE_PROVIDER=http",
                "KOKORO_STORAGE_BASE_URL=http://storage-local:9000",
                "KOKORO_STORAGE_API_KEY=secret",
                "KOKORO_STORAGE_PUBLIC_BASE_URL=http://127.0.0.1:9012",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "KOKORO_LMSTUDIO_MODEL",
        "KOKORO_DEFAULT_PROVIDER_ID",
        "KOKORO_LMSTUDIO_BASE_URL",
        "KOKORO_LMSTUDIO_API_KEY",
        "KOKORO_DEPLOYMENT_MODE",
        "KOKORO_STORAGE_PROVIDER",
        "KOKORO_STORAGE_BASE_URL",
        "KOKORO_STORAGE_API_KEY",
        "KOKORO_STORAGE_PUBLIC_BASE_URL",
        "APP_BASE_URL",
        "PUBLIC_BASE_URL",
        "KOKORO_APP_BASE_URL",
        "KOKORO_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)
    lmstudio = next(
        provider for provider in settings.openai_compatible_providers
        if provider["provider_id"] == "lmstudio"
    )

    assert settings.default_provider_id == "lmstudio"
    assert lmstudio["model"] == "gemma-4-31b-it-uncensored"
    assert lmstudio["base_url"] == "http://127.0.0.1:1234/v1"


def test_legacy_cloud_provider_env_defaults_track_current_model_ids() -> None:
    """The deprecated-env seed tuples must carry the SAME default model
    ids as the catalog builders (``adapter_builders``): runtime_sync's
    ``_legacy_provider_drafts`` persists these values into DB rows on
    first boot, so a retired id here (deepseek-chat 404s after
    2026-07-24; gemini-2.0-flash died 2026-06-01) becomes a dead
    connection that outlives the env var."""
    from kokoro_link.bootstrap.settings import (
        _OPENAI_COMPATIBLE_CLOUD_PROVIDERS,
    )
    from kokoro_link.infrastructure.provider_settings.adapter_builders import (
        _OPENAI_COMPATIBLE_DEFAULTS,
    )

    # Same rename map runtime_sync._legacy_provider_drafts applies when
    # it seeds the rows.
    legacy_to_catalog = {"gemini": "google_gemini"}
    for provider_id, _prefix, _base_url, model in (
        _OPENAI_COMPATIBLE_CLOUD_PROVIDERS
    ):
        catalog_id = legacy_to_catalog.get(provider_id, provider_id)
        assert catalog_id in _OPENAI_COMPATIBLE_DEFAULTS, (
            f"legacy env provider {provider_id!r} has no catalog builder"
        )
        assert model == _OPENAI_COMPATIBLE_DEFAULTS[catalog_id][1], (
            f"legacy env default for {provider_id!r} drifted from the "
            "catalog builder default"
        )


def test_app_settings_loads_http_storage_env(tmp_path: Path, monkeypatch) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            [
                "KOKORO_DEPLOYMENT_MODE=container",
                "KOKORO_STORAGE_PROVIDER=http",
                "KOKORO_STORAGE_BASE_URL=http://storage-local:9000",
                "KOKORO_STORAGE_API_KEY=secret",
                "KOKORO_STORAGE_PUBLIC_BASE_URL=http://127.0.0.1:9012",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "KOKORO_DEPLOYMENT_MODE",
        "KOKORO_STORAGE_PROVIDER",
        "KOKORO_STORAGE_BASE_URL",
        "KOKORO_STORAGE_API_KEY",
        "KOKORO_STORAGE_PUBLIC_BASE_URL",
        "APP_BASE_URL",
        "PUBLIC_BASE_URL",
        "KOKORO_APP_BASE_URL",
        "KOKORO_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.deployment_mode == "container"
    assert settings.storage.provider == "http"
    assert settings.storage.base_url == "http://storage-local:9000"
    assert settings.storage.public_base_url == "http://127.0.0.1:9012"


def test_app_settings_prefers_unprefixed_deployment_env(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/app")
    monkeypatch.setenv("DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("STORAGE_PROVIDER", "http")
    monkeypatch.setenv("STORAGE_URL", "http://storage-local:9000")
    monkeypatch.setenv("STORAGE_KEY", "secret")
    monkeypatch.setenv("STORAGE_PUBLIC_URL", "http://127.0.0.1:9012")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET", "jwt-secret")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "config-secret")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.database_url == "postgresql+asyncpg://user:pass@db:5432/app"
    assert settings.deployment_mode == "container"
    assert settings.storage.base_url == "http://storage-local:9000"
    assert settings.storage.api_key == "secret"
    assert settings.storage.public_base_url == "https://app.example.test"
    assert settings.auth.enabled is True
    assert settings.auth.jwt_secret == "jwt-secret"
    assert settings.config_encryption_key == "config-secret"
    assert settings.public_base_url == "https://app.example.test"


def test_app_settings_loads_cloud_settings(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("STORAGE_PROVIDER", "http")
    monkeypatch.setenv("STORAGE_URL", "http://storage-local:9000")
    monkeypatch.setenv("STORAGE_KEY", "secret")
    monkeypatch.setenv("STORAGE_PUBLIC_URL", "http://127.0.0.1:9012")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YURALUME_CLOUD_USER_SERVICE_URL", "https://users.example")
    monkeypatch.setenv("YURALUME_CLOUD_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_TOKEN", "deploy-secret")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_ID", "hosted-primary")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "yuralume-gateway")
    monkeypatch.setenv("YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", "core-kid|core|yuralume-user|demo-session:release,introspection:session,runtime:read|core-secret")
    monkeypatch.setenv("YURALUME_CLOUD_INTROSPECT_TIMEOUT", "2.5")
    monkeypatch.setenv("YURALUME_CLOUD_SESSION_TTL_SECONDS", "900")
    monkeypatch.setenv("YURALUME_CLOUD_LLM_PRESETS", "chat=gpt-5,summary=sonnet-4.6")
    monkeypatch.setenv("YURALUME_CLOUD_IMAGE_PRESET", "anime-image")
    monkeypatch.setenv("YURALUME_CLOUD_VIDEO_PRESET", "anime-video")
    monkeypatch.setenv("YURALUME_CLOUD_TTS_VOICE", "marin")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.cloud.active is True
    assert settings.cloud.user_service_url == "https://users.example"
    assert settings.cloud.gateway_url == "https://gateway.example"
    assert settings.cloud.deployment_token == "deploy-secret"
    assert settings.cloud.deployment_id == "hosted-primary"
    assert settings.cloud.deployment_audience == "yuralume-gateway"
    assert settings.cloud.internal_service_credential == (
        "core-kid|core|yuralume-user|demo-session:release,introspection:session,runtime:read|core-secret"
    )
    assert settings.cloud.introspect_timeout == 2.5
    assert settings.cloud.session_ttl_seconds == 900
    assert settings.cloud.llm_model_presets == {
        "chat": "gpt-5",
        "summary": "sonnet-4.6",
    }
    assert settings.cloud.image_preset == "anime-image"
    assert settings.cloud.video_preset == "anime-video"
    assert settings.cloud.tts_voice_default == "marin"


@pytest.mark.parametrize(
    ("missing_key", "message"),
    [
        ("YURALUME_CLOUD_USER_SERVICE_URL", "YURALUME_CLOUD_USER_SERVICE_URL"),
        ("YURALUME_CLOUD_GATEWAY_URL", "YURALUME_CLOUD_GATEWAY_URL"),
        ("YURALUME_CLOUD_DEPLOYMENT_TOKEN", "YURALUME_CLOUD_DEPLOYMENT_TOKEN"),
        ("YURALUME_CLOUD_DEPLOYMENT_ID", "YURALUME_CLOUD_DEPLOYMENT_ID"),
        ("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "YURALUME_CLOUD_DEPLOYMENT_AUDIENCE"),
        ("YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", "YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL"),
    ],
)
def test_cloud_mode_requires_service_settings(
    tmp_path: Path,
    monkeypatch,
    missing_key: str,
    message: str,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("STORAGE_PROVIDER", "http")
    monkeypatch.setenv("STORAGE_URL", "http://storage-local:9000")
    monkeypatch.setenv("STORAGE_KEY", "secret")
    monkeypatch.setenv("STORAGE_PUBLIC_URL", "http://127.0.0.1:9012")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YURALUME_CLOUD_USER_SERVICE_URL", "https://users.example")
    monkeypatch.setenv("YURALUME_CLOUD_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_TOKEN", "deploy-secret")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_ID", "hosted-primary")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "yuralume-gateway")
    monkeypatch.setenv("YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", "core-kid|core|yuralume-user|demo-session:release,introspection:session,runtime:read|core-secret")
    monkeypatch.delenv(missing_key, raising=False)

    with pytest.raises(RuntimeError, match=message):
        AppSettings.from_env(project_root=tmp_path)


def test_app_settings_loads_whatsapp_sidecar_url(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("STORAGE_PROVIDER", "http")
    monkeypatch.setenv("STORAGE_URL", "http://storage-local:9000")
    monkeypatch.setenv("STORAGE_KEY", "secret")
    monkeypatch.setenv("STORAGE_PUBLIC_URL", "http://127.0.0.1:9012")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("WHATSAPP_SIDECAR_URL", "http://127.0.0.1:32190/")
    monkeypatch.setenv("WHATSAPP_SIDECAR_API_TOKEN", "sidecar-secret")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.whatsapp_sidecar.base_url == "http://127.0.0.1:32190"
    assert settings.whatsapp_sidecar.api_token == "sidecar-secret"


def test_app_settings_defaults_user_timezone_to_utc(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    monkeypatch.delenv("KOKORO_USER_TIMEZONE", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.user_timezone.default_timezone_id == "UTC"


def test_app_settings_loads_user_timezone(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "KOKORO_USER_TIMEZONE=Asia/Taipei\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    monkeypatch.delenv("KOKORO_USER_TIMEZONE", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.user_timezone.default_timezone_id == "Asia/Taipei"


def test_app_settings_prefers_unprefixed_user_timezone(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "USER_TIMEZONE=Asia/Taipei\n"
        "KOKORO_USER_TIMEZONE=UTC\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    monkeypatch.delenv("KOKORO_USER_TIMEZONE", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.user_timezone.default_timezone_id == "Asia/Taipei"


def test_app_settings_rejects_invalid_user_timezone(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "KOKORO_USER_TIMEZONE=server-local\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    monkeypatch.delenv("KOKORO_USER_TIMEZONE", raising=False)

    with pytest.raises(ValueError, match="KOKORO_USER_TIMEZONE"):
        AppSettings.from_env(project_root=tmp_path)


def test_app_settings_defaults_primary_language_to_zh_tw(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.delenv("USER_PRIMARY_LANGUAGE", raising=False)
    monkeypatch.delenv("KOKORO_USER_PRIMARY_LANGUAGE", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.default_primary_language == "zh-TW"


def test_app_settings_loads_primary_language(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "USER_PRIMARY_LANGUAGE=en-US\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("USER_PRIMARY_LANGUAGE", raising=False)
    monkeypatch.delenv("KOKORO_USER_PRIMARY_LANGUAGE", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.default_primary_language == "en-US"


def test_app_settings_normalises_primary_language_tag(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "USER_PRIMARY_LANGUAGE=ja-jp\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("USER_PRIMARY_LANGUAGE", raising=False)
    monkeypatch.delenv("KOKORO_USER_PRIMARY_LANGUAGE", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.default_primary_language == "ja-JP"


def test_app_settings_prefers_unprefixed_primary_language(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "USER_PRIMARY_LANGUAGE=en-US\n"
        "KOKORO_USER_PRIMARY_LANGUAGE=zh-TW\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("USER_PRIMARY_LANGUAGE", raising=False)
    monkeypatch.delenv("KOKORO_USER_PRIMARY_LANGUAGE", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.default_primary_language == "en-US"


def test_app_settings_rejects_invalid_primary_language(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "USER_PRIMARY_LANGUAGE=123\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("USER_PRIMARY_LANGUAGE", raising=False)
    monkeypatch.delenv("KOKORO_USER_PRIMARY_LANGUAGE", raising=False)

    with pytest.raises(ValueError, match="USER_PRIMARY_LANGUAGE"):
        AppSettings.from_env(project_root=tmp_path)


def test_app_settings_loads_geoip_settings(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "KOKORO_GEOIP_ENABLED=false",
                "KOKORO_GEOIP_PROVIDER=ip-api",
                "KOKORO_GEOIP_ENDPOINT=https://geo.example/json",
                "KOKORO_GEOIP_CACHE_TTL_SECONDS=3600",
                "KOKORO_GEOIP_TIMEOUT_SECONDS=2.5",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "KOKORO_GEOIP_ENABLED",
        "KOKORO_GEOIP_PROVIDER",
        "KOKORO_GEOIP_ENDPOINT",
        "KOKORO_GEOIP_CACHE_TTL_SECONDS",
        "KOKORO_GEOIP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.geoip.enabled is False
    assert settings.geoip.provider == "ip-api"
    assert settings.geoip.endpoint == "https://geo.example/json"
    assert settings.geoip.cache_ttl_seconds == 3600
    assert settings.geoip.timeout_seconds == 2.5


def test_app_settings_uses_app_base_url_when_storage_public_url_is_loopback(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_BASE_URL", "https://kokoro.example.com")
    monkeypatch.setenv("DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("STORAGE_PROVIDER", "http")
    monkeypatch.setenv("STORAGE_URL", "http://storage-local:9000")
    monkeypatch.setenv("STORAGE_KEY", "secret")
    monkeypatch.setenv("STORAGE_PUBLIC_URL", "http://127.0.0.1:9012")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.public_base_url == "https://kokoro.example.com"
    assert settings.storage.public_base_url == "https://kokoro.example.com"


def test_app_settings_keeps_explicit_non_loopback_storage_public_url(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_BASE_URL", "https://kokoro.example.com")
    monkeypatch.setenv("DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("STORAGE_PROVIDER", "http")
    monkeypatch.setenv("STORAGE_URL", "http://storage-local:9000")
    monkeypatch.setenv("STORAGE_KEY", "secret")
    monkeypatch.setenv("STORAGE_PUBLIC_URL", "https://media.example.com")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.public_base_url == "https://kokoro.example.com"
    assert settings.storage.public_base_url == "https://media.example.com"


def test_app_settings_uses_app_base_url_when_storage_public_url_is_blank(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_BASE_URL", "https://kokoro.example.com")
    monkeypatch.setenv("DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("STORAGE_PROVIDER", "http")
    monkeypatch.setenv("STORAGE_URL", "http://storage-local:9000")
    monkeypatch.setenv("STORAGE_KEY", "secret")
    monkeypatch.delenv("STORAGE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("STORAGE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("KOKORO_STORAGE_PUBLIC_BASE_URL", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.storage.public_base_url == "https://kokoro.example.com"


def test_container_mode_requires_http_storage_configuration(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "KOKORO_DEPLOYMENT_MODE=container\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KOKORO_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("KOKORO_STORAGE_PROVIDER", raising=False)
    monkeypatch.delenv("KOKORO_STORAGE_BASE_URL", raising=False)
    monkeypatch.delenv("KOKORO_STORAGE_API_KEY", raising=False)
    monkeypatch.delenv("KOKORO_STORAGE_PUBLIC_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="KOKORO_STORAGE_BASE_URL"):
        AppSettings.from_env(project_root=tmp_path)


def test_local_storage_provider_is_rejected(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "KOKORO_DEPLOYMENT_MODE=local",
                "KOKORO_STORAGE_PROVIDER=local",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("KOKORO_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("KOKORO_STORAGE_PROVIDER", raising=False)

    with pytest.raises(ValueError, match="no longer supported"):
        AppSettings.from_env(project_root=tmp_path)


def test_tts_openai_provider_is_enabled_by_api_key(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "KOKORO_TTS_PROVIDER=openai",
                "KOKORO_TTS_API_KEY=sk-test",
                "KOKORO_TTS_MODEL=gpt-4o-mini-tts",
                "KOKORO_TTS_VOICE_ID=marin",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "KOKORO_TTS_PROVIDER",
        "KOKORO_TTS_API_KEY",
        "KOKORO_TTS_MODEL",
        "KOKORO_TTS_VOICE_ID",
        "KOKORO_TTS_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.tts.enabled is True
    assert settings.tts.provider == "openai"
    assert settings.tts.api_key == "sk-test"
    assert settings.tts.voice_id == "marin"


def test_media_api_settings_are_gateway_key_endpoint_model(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "KOKORO_DEPLOYMENT_MODE=local",
                "KOKORO_STORAGE_PROVIDER=memory",
                "KOKORO_IMAGE_API_BASE_URL=https://media.example/v1",
                "KOKORO_IMAGE_API_KEY=image-key",
                "KOKORO_IMAGE_API_MODEL=nano-banana",
                "KOKORO_IMAGE_API_PROVIDER=gateway",
                "KOKORO_VIDEO_API_BASE_URL=https://media.example/v1",
                "KOKORO_VIDEO_API_KEY=video-key",
                "KOKORO_VIDEO_API_MODEL=veo3",
                "KOKORO_VIDEO_API_PROVIDER=gateway",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "KOKORO_IMAGE_API_BASE_URL",
        "KOKORO_IMAGE_API_KEY",
        "KOKORO_IMAGE_API_MODEL",
        "KOKORO_IMAGE_API_PROVIDER",
        "KOKORO_VIDEO_API_BASE_URL",
        "KOKORO_VIDEO_API_KEY",
        "KOKORO_VIDEO_API_MODEL",
        "KOKORO_VIDEO_API_PROVIDER",
        "KOKORO_DEPLOYMENT_MODE",
        "KOKORO_STORAGE_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.image_api.enabled is True
    assert settings.image_api.model == "nano-banana"
    assert settings.image_api.provider == "gateway"
    assert settings.video_api.enabled is True
    assert settings.video_api.model == "veo3"
    assert settings.video_api.provider == "gateway"


def test_media_api_provider_defaults_endpoint_and_model(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "KOKORO_DEPLOYMENT_MODE=local",
                "KOKORO_STORAGE_PROVIDER=memory",
                "KOKORO_IMAGE_API_PROVIDER=xai",
                "KOKORO_IMAGE_API_KEY=xai-key",
                "KOKORO_VIDEO_API_PROVIDER=google_veo",
                "KOKORO_VIDEO_API_KEY=gemini-key",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "KOKORO_DEPLOYMENT_MODE",
        "KOKORO_STORAGE_PROVIDER",
        "KOKORO_IMAGE_API_PROVIDER",
        "KOKORO_IMAGE_API_KEY",
        "KOKORO_IMAGE_API_BASE_URL",
        "KOKORO_IMAGE_API_MODEL",
        "KOKORO_VIDEO_API_PROVIDER",
        "KOKORO_VIDEO_API_KEY",
        "KOKORO_VIDEO_API_BASE_URL",
        "KOKORO_VIDEO_API_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.image_api.enabled is True
    assert settings.image_api.base_url == "https://api.x.ai/v1"
    assert settings.image_api.model == "grok-imagine-image-quality"
    assert settings.video_api.enabled is True
    assert settings.video_api.base_url == (
        "https://generativelanguage.googleapis.com/v1beta"
    )
    assert settings.video_api.model == "veo-3.1-generate-preview"
