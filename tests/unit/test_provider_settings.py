from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.security.provider_secret_cipher import (
    ProviderSecretCipher,
    ProviderSecretCipherError,
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
        # ComfyUI direct-connect seeds an image row when a server is set;
        # clear the ambient dev-machine value so seed-count assertions are
        # deterministic (Phase 2, CORE_ENV_TO_ADMIN_CONFIG).
        "KOKORO_COMFYUI_SERVER",
        # A non-openai TTS provider is "enabled" as soon as base_url is set,
        # so an ambient dev-machine KOKORO_TTS_BASE_URL would seed a custom_tts
        # row and skew seed-count assertions — clear it for determinism.
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


def _configure_cloud_env(monkeypatch) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "provider-cloud-test-secret-at-least-32-bytes",
    )
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YURALUME_CLOUD_USER_SERVICE_URL", "https://users.example")
    monkeypatch.setenv("YURALUME_CLOUD_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_TOKEN", "deploy-secret")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_ID", "hosted-primary")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "yuralume-gateway")
    monkeypatch.setenv("YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", "core-kid|core|yuralume-user|demo-session:release,introspection:session,runtime:read|core-secret")


def test_openai_compatible_catalog_entries_expose_reasoning_fields() -> None:
    from kokoro_link.infrastructure.provider_settings.catalog import (
        catalog_by_id,
    )

    catalog = catalog_by_id()
    for provider_id in (
        "openai",
        "openrouter",
        "deepseek",
        "mistral",
        "custom_openai_compatible",
        "local_openai_compatible",
    ):
        keys = {f.key for f in catalog[provider_id].config_fields}
        assert {
            "disable_reasoning",
            "reasoning_effort",
            "extra_request_params",
            "strip_think_tags",
        } <= keys, provider_id


def test_anthropic_catalog_entry_exposes_thinking_budget() -> None:
    from kokoro_link.infrastructure.provider_settings.catalog import (
        catalog_by_id,
    )

    keys = {f.key for f in catalog_by_id()["anthropic"].config_fields}
    assert "thinking_budget_tokens" in keys
    # Anthropic must NOT surface the openai_compatible-only knobs.
    assert "disable_reasoning" not in keys
    assert "strip_think_tags" not in keys


def test_config_optional_str_helper() -> None:
    from kokoro_link.contracts.provider_settings import ProviderConnection
    from kokoro_link.infrastructure.provider_settings.runtime_sync import (
        _config_optional_str,
    )

    def _row(cfg: dict) -> ProviderConnection:
        return ProviderConnection(
            id="x",
            provider="openai",
            label="l",
            enabled=True,
            capabilities=("llm",),
            config=cfg,
        )

    assert _config_optional_str(_row({"reasoning_effort": "low"}), "reasoning_effort") == "low"
    assert _config_optional_str(_row({"reasoning_effort": "  "}), "reasoning_effort") is None
    assert _config_optional_str(_row({}), "reasoning_effort") is None


def test_config_optional_json_object_helper() -> None:
    from kokoro_link.contracts.provider_settings import ProviderConnection
    from kokoro_link.infrastructure.provider_settings.runtime_sync import (
        _config_optional_json_object,
    )

    def _row(cfg: dict) -> ProviderConnection:
        return ProviderConnection(
            id="x",
            provider="openai",
            label="l",
            enabled=True,
            capabilities=("llm",),
            config=cfg,
        )

    # valid JSON object
    assert _config_optional_json_object(
        _row({"extra_request_params": '{"top_k": 40}'}),
        "extra_request_params",
    ) == {"top_k": 40}
    # blank / missing → None
    assert _config_optional_json_object(
        _row({"extra_request_params": "   "}), "extra_request_params",
    ) is None
    assert _config_optional_json_object(_row({}), "extra_request_params") is None
    # invalid JSON → None (fail-soft, no raise)
    assert _config_optional_json_object(
        _row({"extra_request_params": "{not json"}), "extra_request_params",
    ) is None
    # JSON array is not an object → None
    assert _config_optional_json_object(
        _row({"extra_request_params": "[1, 2, 3]"}), "extra_request_params",
    ) is None


def test_provider_secret_cipher_roundtrip_and_wrong_key() -> None:
    cipher = ProviderSecretCipher("test-key")
    token = cipher.encrypt({"api_key": "sk-secret", "model": "gpt-image-2"})

    assert "sk-secret" not in token
    assert cipher.decrypt(token) == {
        "api_key": "sk-secret",
        "model": "gpt-image-2",
    }

    wrong = ProviderSecretCipher("other-key")
    try:
        wrong.decrypt(token)
    except ProviderSecretCipherError as exc:
        assert "authentication failed" in str(exc)
    else:
        raise AssertionError("wrong key decrypted provider secret")


def test_admin_provider_catalog_lists_byok_providers(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/admin/providers/catalog")

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert {"openai", "google_gemini", "xai", "google_veo", "custom_tts"} <= ids
    openai = next(row for row in response.json() if row["id"] == "openai")
    assert "embedding" in openai["capabilities"]


def test_cloud_mode_locks_admin_provider_catalog(monkeypatch) -> None:
    _configure_cloud_env(monkeypatch)
    client = TestClient(create_app())
    container = client.app.state.container

    async def seed() -> None:
        # Cloud mode authenticates via the federated strategy, whose
        # verify_token only accepts operator projections with
        # auth_provider="cloud" — seed one so the request reaches the
        # provider-settings lock (403) instead of bouncing at auth (401).
        await container.operator_profile_repository.save(OperatorProfile(
            id="admin",
            display_name="Admin",
            email="admin@example.com",
            is_admin=True,
            auth_provider="cloud",
            cloud_account_id="acct_admin",
            cloud_tenant_id="tenant_admin",
        ))

    asyncio.run(seed())
    token = container.jwt_service.encode("admin")

    response = client.get(
        "/api/v1/admin/providers/catalog",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "provider settings are disabled in cloud mode"


def test_admin_provider_create_redacts_secret(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI prod",
            "enabled": True,
            "capabilities": ["llm", "image", "tts"],
            "config": {
                "base_url": "https://api.openai.com/v1",
                "default_model": "gpt-4o-mini",
                "image_model": "gpt-image-2",
                "tts_model": "gpt-4o-mini-tts",
            },
            "secret": {"api_key": "sk-unit-secret"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "openai"
    assert body["secret"]["configured"] is True
    assert body["secret"]["fingerprint"]
    assert "sk-unit-secret" not in response.text

    listing = client.get("/api/v1/admin/providers")
    assert listing.status_code == 200
    assert "sk-unit-secret" not in listing.text


def test_admin_provider_requires_schema_secret_for_enabled_provider(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI prod",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {"default_model": "gpt-4o-mini"},
            "secret": {},
        },
    )

    assert response.status_code == 400
    assert "secret requires field: api_key" in response.text


def test_admin_provider_rejects_unknown_config_field(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI prod",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {"not_in_catalog": "x"},
            "secret": {"api_key": "sk-unit-secret"},
        },
    )

    assert response.status_code == 400
    assert "does not support field: not_in_catalog" in response.text


def test_admin_provider_test_draft_reports_missing_required_secret(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/admin/providers/test-draft",
        json={
            "provider": "openai",
            "label": "OpenAI draft",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {"default_model": "gpt-4o-mini"},
            "secret": {},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "secret requires field: api_key" in body["last_validation_error"]


def test_admin_provider_create_registers_llm_provider(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    before = client.get("/api/v1/system/providers")
    assert before.status_code == 200
    assert "custom_openai_compatible" not in before.json()

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "custom_openai_compatible",
            "label": "Custom LLM",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {
                "base_url": "https://llm.example.test/v1",
                "default_model": "custom-chat",
            },
            "secret": {"api_key": "sk-unit-secret"},
        },
    )
    assert created.status_code == 201

    providers = client.get("/api/v1/system/providers")
    assert providers.status_code == 200
    assert "custom_openai_compatible" in providers.json()

    disabled = client.patch(
        f"/api/v1/admin/providers/{created.json()['id']}",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    providers_after_disable = client.get("/api/v1/system/providers")
    assert "custom_openai_compatible" not in providers_after_disable.json()


def test_openai_image_model_does_not_shadow_llm_default(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    llm = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI LLM",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {"default_model": "gpt-4o-mini"},
            "secret": {"api_key": "sk-unit-secret"},
        },
    )
    assert llm.status_code == 201

    image = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI Image",
            "enabled": True,
            "capabilities": ["image"],
            "config": {"image_model": "gpt-image-2"},
            "secret": {"api_key": "sk-unit-secret"},
        },
    )
    assert image.status_code == 201

    chat_model = app.state.container.model_registry.resolve("openai")
    assert chat_model._build_payload("hi")["model"] == "gpt-4o-mini"  # noqa: SLF001
    image_profile = app.state.container.image_profile_registry.get_profile("openai")
    assert image_profile is not None
    assert image_profile.api.model == "gpt-image-2"


def test_admin_provider_create_registers_image_and_video_profiles(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    image = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "xai",
            "label": "Grok Image",
            "enabled": True,
            "capabilities": ["image"],
            "config": {"default_model": "grok-2-image-1212"},
            "secret": {"api_key": "xai-secret"},
        },
    )
    assert image.status_code == 201
    image_profiles = client.get("/api/v1/system/image-profiles")
    assert image_profiles.status_code == 200
    assert image_profiles.json() == [
        {"id": "xai", "label": "Grok Image", "kind": "external_api"},
    ]

    video = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "google_veo",
            "label": "Veo",
            "enabled": True,
            "capabilities": ["video"],
            "config": {"default_model": "veo-3.1-generate-preview"},
            "secret": {"api_key": "veo-secret"},
        },
    )
    assert video.status_code == 201
    video_profiles = client.get("/api/v1/system/video-profiles")
    assert video_profiles.status_code == 200
    assert video_profiles.json() == [
        {"id": "google_veo", "label": "Veo", "kind": "external_api"},
    ]


def test_admin_provider_create_registers_tts_catalog(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI TTS",
            "enabled": True,
            "capabilities": ["tts"],
            "config": {
                "tts_model": "gpt-4o-mini-tts",
                "voice_id": "marin",
            },
            "secret": {"api_key": "sk-tts-secret"},
        },
    )
    assert created.status_code == 201

    response = client.get("/api/v1/tts/assets")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    voice_ids = {row["voice_id"] for row in body["voice_presets"]}
    assert "marin" in voice_ids


def test_admin_provider_create_registers_embedding_backend(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    assert app.state.container.embedder.is_operational is False
    assert app.state.container.chat_service._embedder.is_operational is False  # noqa: SLF001

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "local_openai_compatible",
            "label": "Local embedding",
            "enabled": True,
            "capabilities": ["embedding"],
            "config": {
                "base_url": "http://127.0.0.1:1234/v1",
                "embedding_model": "text-embedding-bge-m3",
                "embedding_dimension": 1024,
            },
            "secret": {},
        },
    )
    assert created.status_code == 201

    assert app.state.container.embedder.is_operational is True
    assert app.state.container.embedder.dimension == 1024
    assert app.state.container.chat_service._embedder.is_operational is True  # noqa: SLF001

    disabled = client.patch(
        f"/api/v1/admin/providers/{created.json()['id']}",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert app.state.container.embedder.is_operational is False


def test_embedding_dimension_mismatch_does_not_enable_backend(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "local_openai_compatible",
            "label": "Wrong embedding dimension",
            "enabled": True,
            "capabilities": ["embedding"],
            "config": {
                "base_url": "http://127.0.0.1:1234/v1",
                "embedding_model": "text-embedding-bge-m3",
                "embedding_dimension": 1536,
            },
            "secret": {},
        },
    )

    assert created.status_code == 201
    assert app.state.container.embedder.is_operational is False


def test_latest_enabled_tts_provider_becomes_runtime_backend(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    custom = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "custom_tts",
            "label": "Custom TTS",
            "enabled": True,
            "capabilities": ["tts"],
            "config": {
                "base_url": "https://tts.example.test/v1",
                "default_model": "custom-tts",
                "voice_id": "remote-voice",
            },
            "secret": {"api_key": "custom-secret"},
        },
    )
    assert custom.status_code == 201

    openai = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI TTS",
            "enabled": True,
            "capabilities": ["tts"],
            "config": {
                "tts_model": "gpt-4o-mini-tts",
                "voice_id": "marin",
            },
            "secret": {"api_key": "sk-tts-secret"},
        },
    )
    assert openai.status_code == 201

    response = client.get("/api/v1/tts/assets")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    voice_ids = {row["voice_id"] for row in body["voice_presets"]}
    assert "marin" in voice_ids


def test_blank_secret_update_preserves_existing_secret(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())
    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI prod",
            "capabilities": ["llm"],
            "secret": {"api_key": "sk-original-secret"},
        },
    ).json()
    original_fingerprint = created["secret"]["fingerprint"]

    updated = client.patch(
        f"/api/v1/admin/providers/{created['id']}",
        json={
            "label": "OpenAI renamed",
            "secret": {"api_key": ""},
        },
    )

    assert updated.status_code == 200
    body = updated.json()
    assert body["label"] == "OpenAI renamed"
    assert body["secret"]["configured"] is True
    assert body["secret"]["fingerprint"] == original_fingerprint


def test_clear_secret_removes_stored_secret(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())
    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "custom_tts",
            "label": "TTS",
            "capabilities": ["tts"],
            "config": {
                "base_url": "https://tts.example.test/v1",
                "default_model": "custom-tts",
            },
            "secret": {"api_key": "tts-secret"},
        },
    ).json()

    updated = client.patch(
        f"/api/v1/admin/providers/{created['id']}",
        json={"clear_secret": True},
    )

    assert updated.status_code == 200
    assert updated.json()["secret"] == {"configured": False, "fingerprint": ""}


def test_clear_required_secret_from_enabled_provider_is_rejected(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())
    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI",
            "capabilities": ["llm"],
            "secret": {"api_key": "sk-original-secret"},
        },
    ).json()

    updated = client.patch(
        f"/api/v1/admin/providers/{created['id']}",
        json={"clear_secret": True},
    )

    assert updated.status_code == 400
    assert "secret requires field: api_key" in updated.text


def test_legacy_provider_env_seeds_empty_provider_connections(monkeypatch) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setenv("KOKORO_OPENAI_API_KEY", "sk-legacy-openai")
    monkeypatch.setenv("KOKORO_OPENAI_MODEL", "gpt-4o-mini")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/providers")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["provider"] == "openai"
    assert body[0]["capabilities"] == ["llm"]
    assert body[0]["secret"]["configured"] is True
    assert "sk-legacy-openai" not in response.text


def test_legacy_embedding_env_seeds_empty_provider_connections(monkeypatch) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-bge-m3")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-secret")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/providers")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["provider"] == "local_openai_compatible"
    assert body[0]["capabilities"] == ["embedding"]
    assert body[0]["config"]["embedding_model"] == "text-embedding-bge-m3"
    assert body[0]["secret"]["configured"] is True
    assert "embedding-secret" not in response.text


# ---------------------------------------------------------------------------
# ComfyUI direct-connect (Phase 2, CORE_ENV_TO_ADMIN_CONFIG). Catalog entry,
# env seeding, and comfyui-kind profile materialisation.
# ---------------------------------------------------------------------------


def test_comfyui_catalog_entry_is_image_only_no_auth() -> None:
    from kokoro_link.infrastructure.provider_settings.catalog import (
        catalog_by_id,
    )

    entry = catalog_by_id()["comfyui"]
    assert entry.capabilities == ("image",)
    assert entry.adapter_kind == "comfyui"
    # No auth fields — local ComfyUI has no API key.
    assert entry.auth_fields == ()
    keys = {f.key for f in entry.config_fields}
    assert {"server", "checkpoint", "workflow_file", "lora_dir"} <= keys
    server = next(f for f in entry.config_fields if f.key == "server")
    assert server.required is True
    checkpoint = next(f for f in entry.config_fields if f.key == "checkpoint")
    # Dropdown hint kind so the admin form fetches /object_info.
    assert checkpoint.kind == "comfyui_checkpoint"


def test_legacy_comfyui_env_seeds_image_connection(monkeypatch) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setenv("KOKORO_COMFYUI_SERVER", "http://127.0.0.1:8188")
    monkeypatch.setenv("KOKORO_COMFYUI_CHECKPOINT", "waiNSFW_v14.safetensors")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/providers")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["provider"] == "comfyui"
    assert body[0]["capabilities"] == ["image"]
    assert body[0]["config"]["server"] == "http://127.0.0.1:8188"
    assert body[0]["config"]["checkpoint"] == "waiNSFW_v14.safetensors"


def test_comfyui_row_materialises_comfyui_image_profile(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "comfyui",
            "label": "Local ComfyUI",
            "enabled": True,
            "capabilities": ["image"],
            "config": {
                "server": "http://127.0.0.1:8188",
                "checkpoint": "waiNSFW_v14.safetensors",
            },
            "secret": {},
        },
    )
    assert created.status_code == 201

    registry = app.state.container.image_profile_registry
    comfy_profiles = [p for p in registry.profiles if p.kind == "comfyui"]
    assert len(comfy_profiles) == 1
    profile = comfy_profiles[0]
    assert profile.comfyui is not None
    assert profile.comfyui.server == "http://127.0.0.1:8188"
    assert profile.comfyui.checkpoint == "waiNSFW_v14.safetensors"


# ---------------------------------------------------------------------------
# Provider capability expansion (OpenRouter full-capability + NanoGPT preset).
# Owner ratified 2026-07-05; endpoints verified against live docs.
# ---------------------------------------------------------------------------


def test_openrouter_catalog_exposes_full_capabilities() -> None:
    from kokoro_link.infrastructure.provider_settings.catalog import (
        catalog_by_id,
    )

    entry = catalog_by_id()["openrouter"]
    assert set(entry.capabilities) == {"llm", "embedding", "tts", "image"}
    # video is deliberately absent — async job API, not wired this wave.
    assert "video" not in entry.capabilities
    keys = {f.key for f in entry.config_fields}
    assert {"embedding_model", "embedding_dimension", "request_dimensions"} <= keys
    assert {"tts_model", "voice_id", "response_format"} <= keys
    assert "image_model" in keys


def test_nanogpt_catalog_entry_is_llm_and_image_preset() -> None:
    from kokoro_link.infrastructure.provider_settings.catalog import (
        catalog_by_id,
    )

    entry = catalog_by_id()["nanogpt"]
    assert entry.capabilities == ("llm", "image")
    assert entry.adapter_kind == "openai_compatible"
    keys = {f.key for f in entry.config_fields}
    assert "image_model" in keys


def test_openrouter_embedding_registers_lmstudio_backend(monkeypatch) -> None:
    from kokoro_link.infrastructure.embedder.lm_studio import LMStudioEmbedder

    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openrouter",
            "label": "OpenRouter embedding",
            "enabled": True,
            "capabilities": ["embedding"],
            "config": {
                "embedding_model": "baai/bge-m3",
                "embedding_dimension": 1024,
            },
            "secret": {"api_key": "sk-or-secret"},
        },
    )
    assert created.status_code == 201

    embedder = app.state.container.embedder
    assert embedder.is_operational is True
    assert embedder.dimension == 1024
    backend = embedder._backend  # noqa: SLF001
    assert isinstance(backend, LMStudioEmbedder)
    assert backend._base_url == "https://openrouter.ai/api/v1"  # noqa: SLF001


def test_openrouter_embedding_dimension_mismatch_is_rejected(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openrouter",
            "label": "OpenRouter bad dim",
            "enabled": True,
            "capabilities": ["embedding"],
            "config": {
                "embedding_model": "some/1536-dim-model",
                "embedding_dimension": 1536,
            },
            "secret": {"api_key": "sk-or-secret"},
        },
    )

    assert created.status_code == 201
    # Same safe-failure as other embedding providers — the hard 1024
    # constraint is not bypassed for the new provider.
    assert app.state.container.embedder.is_operational is False


def test_openrouter_tts_registers_openai_speech_adapter(monkeypatch) -> None:
    from kokoro_link.infrastructure.tts.external_api import OpenAITTSAdapter

    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openrouter",
            "label": "OpenRouter TTS",
            "enabled": True,
            "capabilities": ["tts"],
            "config": {
                "tts_model": "openai/gpt-4o-mini-tts",
                "voice_id": "alloy",
            },
            "secret": {"api_key": "sk-or-secret"},
        },
    )
    assert created.status_code == 201

    # OpenRouter proxies OpenAI's /audio/speech → the OpenAI-speech
    # adapter, NOT the generic gateway ExternalTTSAdapter.
    catalog_port = app.state.container.tts_voice_catalog
    assert isinstance(catalog_port, OpenAITTSAdapter)

    assets = client.get("/api/v1/tts/assets")
    assert assets.status_code == 200
    body = assets.json()
    assert body["enabled"] is True
    voice_ids = {row["voice_id"] for row in body["voice_presets"]}
    assert "alloy" in voice_ids


def test_openrouter_image_registers_openrouter_provider(monkeypatch) -> None:
    from kokoro_link.infrastructure.image.openrouter_provider import (
        OpenRouterImageProvider,
    )

    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openrouter",
            "label": "OpenRouter Image",
            "enabled": True,
            "capabilities": ["image"],
            "config": {"image_model": "black-forest-labs/flux.2-pro"},
            "secret": {"api_key": "sk-or-secret"},
        },
    )
    assert created.status_code == 201

    registry = app.state.container.image_profile_registry
    profile = registry.get_profile("openrouter")
    assert profile is not None
    assert profile.api.provider == "openrouter"
    assert profile.api.model == "black-forest-labs/flux.2-pro"
    built = registry.resolve("openrouter")
    assert isinstance(built, OpenRouterImageProvider)


def test_openrouter_video_capability_is_rejected(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openrouter",
            "label": "OpenRouter Video",
            "enabled": True,
            "capabilities": ["video"],
            "config": {"default_model": "veo-3.1"},
            "secret": {"api_key": "sk-or-secret"},
        },
    )

    # video is not in OpenRouter's catalog capabilities → server-side reject.
    assert created.status_code == 400
    assert "does not support capability: video" in created.text


def test_nanogpt_llm_registers_openai_compatible_model(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "nanogpt",
            "label": "NanoGPT chat",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {},
            "secret": {"api_key": "nano-secret"},
        },
    )
    assert created.status_code == 201

    providers = client.get("/api/v1/system/providers")
    assert providers.status_code == 200
    assert "nanogpt" in providers.json()

    chat_model = app.state.container.model_registry.resolve("nanogpt")
    # base_url / default_model fall back to _OPENAI_COMPATIBLE_DEFAULTS.
    assert chat_model._base_url == "https://nano-gpt.com/api/v1"  # noqa: SLF001
    assert chat_model._build_payload("hi")["model"] == "gpt-5.2"  # noqa: SLF001


def test_nanogpt_image_registers_gateway_profile(monkeypatch) -> None:
    from kokoro_link.infrastructure.image.external_api_provider import (
        ExternalImageApiProvider,
    )

    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "nanogpt",
            "label": "NanoGPT Image",
            "enabled": True,
            "capabilities": ["image"],
            "config": {"image_model": "flux-1.1-pro"},
            "secret": {"api_key": "nano-secret"},
        },
    )
    assert created.status_code == 201

    registry = app.state.container.image_profile_registry
    profile = registry.get_profile("nanogpt")
    assert profile is not None
    # gateway kind → ExternalImageApiProvider (OpenAI-Images shape).
    assert profile.api.provider == "gateway"
    assert profile.api.model == "flux-1.1-pro"
    built = registry.resolve("nanogpt")
    assert isinstance(built, ExternalImageApiProvider)


# ---------------------------------------------------------------------------
# Web-search `search` capability (Tavily / SearXNG / DuckDuckGo BYOK).
# ---------------------------------------------------------------------------


def test_search_catalog_entries_are_present() -> None:
    from kokoro_link.infrastructure.provider_settings.catalog import (
        catalog_by_id,
    )

    catalog = catalog_by_id()
    for provider_id in ("tavily", "searxng", "duckduckgo"):
        assert provider_id in catalog, provider_id
        assert catalog[provider_id].capabilities == ("search",), provider_id

    # tavily: api_key required.
    tavily = catalog["tavily"]
    assert any(f.key == "api_key" and f.required for f in tavily.auth_fields)

    # searxng: base_url required, api_key optional.
    searxng = catalog["searxng"]
    assert any(f.key == "api_key" and not f.required for f in searxng.auth_fields)
    assert any(f.key == "base_url" and f.required for f in searxng.config_fields)

    # duckduckgo: no auth at all.
    duckduckgo = catalog["duckduckgo"]
    assert duckduckgo.auth_fields == ()


def _web_search_registered(app) -> bool:
    return app.state.container.tool_registry.get("web_search") is not None


def test_search_provider_create_registers_web_search_tool(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    # No search provider configured out of the box → tool absent.
    assert _web_search_registered(app) is False

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "duckduckgo",
            "label": "DDG search",
            "enabled": True,
            "capabilities": ["search"],
            "config": {},
            "secret": {},
        },
    )
    assert created.status_code == 201
    assert _web_search_registered(app) is True


def test_search_provider_switch_replaces_and_disable_removes(monkeypatch) -> None:
    from kokoro_link.infrastructure.tools.websearch import (
        DuckDuckGoSearchClient,
        SearXNGSearchClient,
    )

    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    ddg = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "duckduckgo",
            "label": "DDG",
            "enabled": True,
            "capabilities": ["search"],
            "config": {},
            "secret": {},
        },
    )
    assert ddg.status_code == 201
    tool = app.state.container.tool_registry.get("web_search")
    assert isinstance(tool._client, DuckDuckGoSearchClient)  # noqa: SLF001

    # A newer enabled searxng row wins (most-recently-updated).
    searxng = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "searxng",
            "label": "SearXNG",
            "enabled": True,
            "capabilities": ["search"],
            "config": {"base_url": "https://searxng.example.test"},
            "secret": {},
        },
    )
    assert searxng.status_code == 201
    tool = app.state.container.tool_registry.get("web_search")
    assert isinstance(tool._client, SearXNGSearchClient)  # noqa: SLF001

    # Disable both → tool removed.
    for created in (ddg, searxng):
        disabled = client.patch(
            f"/api/v1/admin/providers/{created.json()['id']}",
            json={"enabled": False},
        )
        assert disabled.status_code == 200
    assert _web_search_registered(app) is False


def test_openai_web_search_catalog_entry() -> None:
    from kokoro_link.infrastructure.provider_settings.catalog import (
        catalog_by_id,
    )

    entry = catalog_by_id()["openai_web_search"]
    assert entry.capabilities == ("search",)
    # api_key required; search_model required only when search is selected.
    assert any(f.key == "api_key" and f.required for f in entry.auth_fields)
    config_keys = {f.key for f in entry.config_fields}
    assert {"search_model", "search_context_size", "search_tool_type"} <= config_keys
    search_model = next(f for f in entry.config_fields if f.key == "search_model")
    assert "search" in search_model.required_for_capabilities
    # adapter_kind=openai lets the search_model field reuse model discovery,
    # but dispatch is by provider id so it is never treated as an LLM.
    assert entry.adapter_kind == "openai"
    assert "gpt-5.4-mini" in entry.default_models


def test_openai_web_search_provider_registers_and_builds_client(monkeypatch) -> None:
    from kokoro_link.infrastructure.tools.websearch import OpenAIWebSearchClient

    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai_web_search",
            "label": "OpenAI web search",
            "enabled": True,
            "capabilities": ["search"],
            "config": {"search_model": "gpt-5.4-mini"},
            "secret": {"api_key": "sk-openai-secret"},
        },
    )
    assert created.status_code == 201
    tool = app.state.container.tool_registry.get("web_search")
    assert tool is not None
    assert isinstance(tool._client, OpenAIWebSearchClient)  # noqa: SLF001
    # Secret never echoed back.
    assert "sk-openai-secret" not in created.text


def test_openai_web_search_requires_model(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    # search_model is required when the search capability is selected.
    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai_web_search",
            "label": "OpenAI no model",
            "enabled": True,
            "capabilities": ["search"],
            "config": {},
            "secret": {"api_key": "sk-openai-secret"},
        },
    )
    assert created.status_code == 400
    assert "search_model" in created.text


def test_searxng_search_requires_base_url(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    # Server-side field validation: base_url is required for searxng.
    created = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "searxng",
            "label": "SearXNG no url",
            "enabled": True,
            "capabilities": ["search"],
            "config": {},
            "secret": {},
        },
    )
    assert created.status_code == 400
    assert "base_url" in created.text


def test_legacy_tavily_env_seeds_search_connection(monkeypatch) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-legacy-secret")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/providers")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["provider"] == "tavily"
    assert body[0]["capabilities"] == ["search"]
    assert body[0]["secret"]["configured"] is True
    assert "tvly-legacy-secret" not in response.text
    # Seeded row hot-wires the tool on boot.
    assert _web_search_registered(app) is True


def test_legacy_custom_tts_env_seeds_single_tts_connection(monkeypatch) -> None:
    # Regression: a custom (non-openai) KOKORO_TTS_* env must seed exactly one
    # tts row. The legacy draft used to inject response_format, which the
    # custom_tts catalog does not accept, so create_connection rejected the
    # whole draft and the row was silently dropped (WARNING at boot).
    _configure_env(monkeypatch)
    monkeypatch.setenv("KOKORO_TTS_PROVIDER", "custom")
    monkeypatch.setenv("KOKORO_TTS_BASE_URL", "https://tts.example.test/v1")
    monkeypatch.setenv("KOKORO_TTS_MODEL", "custom-tts")
    monkeypatch.setenv("KOKORO_TTS_VOICE_ID", "narrator")
    # Even with a response_format configured, the custom draft must omit it
    # rather than let it poison the whole row.
    monkeypatch.setenv("KOKORO_TTS_RESPONSE_FORMAT", "mp3")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/providers")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    row = body[0]
    assert row["provider"] == "custom_tts"
    assert row["capabilities"] == ["tts"]
    assert row["config"]["base_url"] == "https://tts.example.test/v1"
    # The offending field the custom_tts catalog rejects must not be present.
    assert "response_format" not in row["config"]
