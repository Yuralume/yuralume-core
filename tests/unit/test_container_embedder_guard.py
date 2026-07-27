"""Embedder wiring stays independent from BYOK chat provider setup."""

from __future__ import annotations

from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings, CloudSettings, EmbeddingSettings
from kokoro_link.infrastructure.embedder.cloud_gateway import CloudGatewayEmbedder


def test_fake_provider_without_embedder_builds_ok() -> None:
    settings = AppSettings(
        default_provider_id="fake",
        database_url="",
        openai_compatible_providers=(),
        embedding=EmbeddingSettings(),  # unset → null embedder is fine
    )
    container = build_container(settings)
    # Null embedder is in use — memorializer/chat write paths pass through
    assert container.chat_service._embedder.is_operational is False  # noqa: SLF001


def test_legacy_real_provider_without_embedder_still_builds() -> None:
    settings = AppSettings(
        default_provider_id="lmstudio",
        database_url="",
        openai_compatible_providers=(
            {
                "provider_id": "lmstudio",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "lm-studio",
                "model": "some-model",
            },
        ),
        embedding=EmbeddingSettings(),
    )

    container = build_container(settings)

    assert container.chat_service._embedder.is_operational is False  # noqa: SLF001


def test_real_provider_with_embedder_builds_ok() -> None:
    settings = AppSettings(
        default_provider_id="fake",
        database_url="",
        embedding=EmbeddingSettings(
            model="text-embedding-bge-m3",
            base_url="http://127.0.0.1:1234/v1",
            api_key="lm-studio",
            dimension=1024,
        ),
    )
    container = build_container(settings)
    assert container.chat_service._embedder.is_operational is True  # noqa: SLF001


def test_cloud_mode_with_embedder_uses_cloud_gateway_backend() -> None:
    settings = AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
            embedding_preset="profile-embedding",
        ),
        embedding=EmbeddingSettings(
            model="legacy-local-model",
            base_url="http://127.0.0.1:1234/v1",
            dimension=1024,
        ),
    )

    container = build_container(settings)

    assert isinstance(
        container.chat_service._embedder._backend,  # noqa: SLF001
        CloudGatewayEmbedder,
    )
