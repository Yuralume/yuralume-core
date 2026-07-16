import pytest

from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import (
    AppSettings,
    CloudSettings,
    ObjectStorageSettings,
    PromptQualitySettings,
    UserTimezoneSettings,
)
from kokoro_link.application.services.cloud_active_llm_provider import (
    CloudActiveLLMProvider,
)
from kokoro_link.application.services.messaging_public_url import (
    MESSAGING_PUBLIC_BASE_URL_KEY,
)
from kokoro_link.infrastructure.messaging.telegram.adapter import TelegramAdapter
from kokoro_link.infrastructure.prompt.llm_material_digester import (
    LLMPromptMaterialDigester,
)
from kokoro_link.infrastructure.prompt.llm_novelty_gate import LLMNoveltyGate
from kokoro_link.infrastructure.prompt.null_material_digester import (
    NullPromptMaterialDigester,
)
from kokoro_link.infrastructure.prompt.null_novelty_gate import NullNoveltyGate
from kokoro_link.infrastructure.register.llm_register_profiler import (
    LLMRegisterProfiler,
)
from kokoro_link.infrastructure.register.null_register_profiler import (
    NullRegisterProfiler,
)
from kokoro_link.infrastructure.usage.llm_metering import MeteredActiveLLMProvider


def test_legacy_llm_env_config_does_not_register_runtime_provider() -> None:
    settings = AppSettings(
        default_provider_id="lmstudio",
        openai_compatible_providers=(
            {
                "provider_id": "lmstudio",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "lm-studio",
                "model": "local-model",
            },
        ),
    )

    container = build_container(settings)

    assert container.model_registry.list_ids() == ["fake"]


def test_container_schedule_timezone_comes_from_settings() -> None:
    settings = AppSettings(
        user_timezone=UserTimezoneSettings(default_timezone_id="Asia/Taipei"),
    )

    container = build_container(settings)

    assert getattr(container.schedule_service.local_tz, "key", None) == "Asia/Taipei"


def test_container_uses_cloud_active_llm_provider_in_cloud_mode() -> None:
    settings = AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
            llm_model_presets={"chat": "preset-chat"},
        ),
    )

    container = build_container(settings)

    assert isinstance(container.active_llm_provider, MeteredActiveLLMProvider)
    assert isinstance(container.active_llm_provider.inner, CloudActiveLLMProvider)


def test_container_wires_usage_recorder_after_feed_composer_is_created() -> None:
    settings = AppSettings(database_url="")

    container = build_container(settings)

    assert container.feed_composer_service is not None
    feed_usage_recorder = container.feed_composer_service._usage_recorder  # noqa: SLF001
    assert feed_usage_recorder is not None
    assert feed_usage_recorder._repository is container.usage_event_repository  # noqa: SLF001


def test_container_wires_notification_service_to_push_surfaces() -> None:
    settings = AppSettings(database_url="")

    container = build_container(settings)

    assert container.notification_service is not None
    assert container.web_push_subscription_repository is not None
    assert container.notification_preferences_repository is not None
    assert container.proactive_dispatcher is not None
    assert container.feed_composer_service is not None
    assert container.feed_comment_reply_service is not None
    assert (
        container.proactive_dispatcher._notification_service  # noqa: SLF001
        is container.notification_service
    )
    assert (
        container.feed_composer_service._notification_service  # noqa: SLF001
        is container.notification_service
    )
    assert (
        container.feed_comment_reply_service._notification_service  # noqa: SLF001
        is container.notification_service
    )


def test_container_wires_schedule_service_into_feed_composer() -> None:
    settings = AppSettings(database_url="")

    container = build_container(settings)

    assert container.feed_composer_service is not None
    assert (
        container.feed_composer_service._schedule_service  # noqa: SLF001
        is container.schedule_service
    )


def test_container_wires_background_encounter_and_schedule_memorializer() -> None:
    settings = AppSettings(database_url="")

    container = build_container(settings)

    assert container.proactive_scheduler is not None
    assert (
        container.proactive_scheduler._character_encounter_service  # noqa: SLF001
        is container.character_encounter_service
    )
    assert (
        container.proactive_scheduler._schedule_memorializer  # noqa: SLF001
        is container.schedule_memorializer
    )
    assert container.demo_account_reaper is not None
    assert (
        container.proactive_scheduler._demo_account_reaper  # noqa: SLF001
        is container.demo_account_reaper
    )


@pytest.mark.asyncio
async def test_container_wires_messaging_public_url_resolver() -> None:
    settings = AppSettings(
        database_url="",
        public_base_url="http://127.0.0.1:8012",
    )

    container = build_container(settings)
    await container.preferences_repository.set(
        MESSAGING_PUBLIC_BASE_URL_KEY,
        "https://public.example.test/",
    )

    assert container.messaging_dispatcher is not None
    assert container.proactive_dispatcher is not None
    assert (
        await container.messaging_dispatcher._resolve_public_base_url()  # noqa: SLF001
        == "https://public.example.test"
    )
    assert (
        await container.proactive_dispatcher._resolve_public_base_url()  # noqa: SLF001
        == "https://public.example.test"
    )


@pytest.mark.asyncio
async def test_container_wires_telegram_adapter_to_object_storage() -> None:
    settings = AppSettings(
        database_url="",
        storage=ObjectStorageSettings(provider="memory"),
    )
    container = build_container(settings)

    await container.object_storage.put_bytes(
        object_key="characters/mio/tg-photo.png",
        content=b"telegram-image",
        content_type="image/png",
    )

    assert container.messaging_dispatcher is not None
    adapter = container.messaging_dispatcher._adapters["telegram"]  # noqa: SLF001
    assert isinstance(adapter, TelegramAdapter)
    assert adapter._local_image_fetcher is not None  # noqa: SLF001

    result = await adapter._local_image_fetcher(  # noqa: SLF001
        "https://public.example.test/v1/public/characters/mio/tg-photo.png",
    )

    assert result is not None
    assert result.handled is True
    assert result.content == b"telegram-image"


def test_persona_curiosity_flags_load_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("PERSONA_CURIOSITY_ENABLED", "false")
    monkeypatch.setenv("PERSONA_CURIOSITY_PROACTIVE_ENABLED", "false")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.persona.curiosity_enabled is False
    assert settings.persona.curiosity_proactive_enabled is False


def test_prompt_quality_flags_load_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("KOKORO_PROMPT_MATERIAL_DIGEST_ENABLED", "true")
    monkeypatch.setenv("KOKORO_NOVELTY_GATE_ENABLED", "true")
    monkeypatch.setenv("KOKORO_NOVELTY_GATE_MAX_RETRIES", "2")
    monkeypatch.setenv("KOKORO_REGISTER_PROFILE_ENABLED", "true")
    monkeypatch.setenv("KOKORO_REPLY_QUALITY_GATE_RISK_THRESHOLD", "0.7")
    monkeypatch.setenv("KOKORO_REPLY_QUALITY_SIMILARITY_THRESHOLD", "0.9")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.prompt_quality == PromptQualitySettings(
        material_digest_enabled=True,
        novelty_gate_enabled=True,
        novelty_gate_max_retries=2,
        register_profile_enabled=True,
        reply_quality_gate_risk_threshold=0.7,
        reply_quality_similarity_threshold=0.9,
    )


def test_prompt_quality_flags_default_to_enabled_with_risk_gate() -> None:
    settings = AppSettings(database_url="")

    assert settings.prompt_quality == PromptQualitySettings(
        material_digest_enabled=True,
        novelty_gate_enabled=True,
        novelty_gate_max_retries=1,
        register_profile_enabled=True,
        reply_quality_gate_risk_threshold=0.65,
        reply_quality_similarity_threshold=0.88,
    )


def test_container_uses_null_material_digester_when_disabled_or_fake() -> None:
    disabled = build_container(AppSettings(database_url=""))
    fake_enabled = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(material_digest_enabled=True),
        ),
    )

    assert isinstance(
        disabled.chat_service._prompt_material_digester,  # noqa: SLF001
        NullPromptMaterialDigester,
    )
    assert isinstance(
        fake_enabled.chat_service._prompt_material_digester,  # noqa: SLF001
        NullPromptMaterialDigester,
    )


def test_container_wires_llm_material_digester_when_enabled_with_real_provider() -> None:
    settings = AppSettings(
        database_url="",
        default_provider_id="lmstudio",
        prompt_quality=PromptQualitySettings(material_digest_enabled=True),
    )

    container = build_container(settings)

    assert isinstance(
        container.chat_service._prompt_material_digester,  # noqa: SLF001
        LLMPromptMaterialDigester,
    )


def test_container_uses_null_novelty_gate_when_disabled_or_fake() -> None:
    disabled = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(novelty_gate_enabled=False),
        ),
    )
    fake_enabled = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(novelty_gate_enabled=True),
        ),
    )

    assert isinstance(
        disabled.chat_service._novelty_gate,  # noqa: SLF001
        NullNoveltyGate,
    )
    assert isinstance(
        fake_enabled.chat_service._novelty_gate,  # noqa: SLF001
        NullNoveltyGate,
    )


def test_container_wires_llm_novelty_gate_when_enabled_with_real_provider() -> None:
    settings = AppSettings(
        database_url="",
        default_provider_id="lmstudio",
        prompt_quality=PromptQualitySettings(novelty_gate_enabled=True),
    )

    container = build_container(settings)

    assert isinstance(
        container.chat_service._novelty_gate,  # noqa: SLF001
        LLMNoveltyGate,
    )
    assert container.chat_service._novelty_gate_max_retries == 1  # noqa: SLF001


def test_container_uses_null_register_profiler_when_disabled_or_fake() -> None:
    disabled = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(register_profile_enabled=False),
        ),
    )
    fake_enabled = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(register_profile_enabled=True),
        ),
    )

    assert isinstance(
        disabled.chat_service._register_profiler,  # noqa: SLF001
        NullRegisterProfiler,
    )
    assert isinstance(
        fake_enabled.chat_service._register_profiler,  # noqa: SLF001
        NullRegisterProfiler,
    )


def test_container_wires_llm_register_profiler_when_enabled_with_real_provider() -> None:
    settings = AppSettings(
        database_url="",
        default_provider_id="lmstudio",
        prompt_quality=PromptQualitySettings(register_profile_enabled=True),
    )

    container = build_container(settings)

    assert isinstance(
        container.chat_service._register_profiler,  # noqa: SLF001
        LLMRegisterProfiler,
    )
    assert container.chat_service._register_profile_enabled is True  # noqa: SLF001
    assert (  # noqa: SLF001
        container.chat_service._reply_quality_gate_risk_threshold == 0.65
    )


def test_container_wires_operator_profile_service_into_character_encounter() -> None:
    """I18N_HARDENING_PLAN #5/#6: encounter fallback strings and prompt
    hints must resolve the owning operator's ``primary_language`` via
    the shared ``operator_profile_service``, not silently default to
    zh-TW because the container forgot to pass it through."""
    settings = AppSettings(database_url="")

    container = build_container(settings)

    assert container.character_encounter_service is not None
    planner = container.character_encounter_service._planner  # noqa: SLF001
    runner = container.character_encounter_service._runner  # noqa: SLF001
    assert (
        planner._operator_profile_service  # noqa: SLF001
        is container.operator_profile_service
    )
    assert (
        runner._operator_profile_service  # noqa: SLF001
        is container.operator_profile_service
    )


def test_container_wires_operator_profile_service_into_persona_projection() -> None:
    """I18N_HARDENING_PLAN #7: the persona-projection narrative must
    follow the owning operator's ``primary_language`` instead of always
    falling back to zh-TW because the container omitted the kwarg.

    ``OperatorPersonaProjectionService`` is only constructed inside the
    ``if operator_persona_service is not None:`` DB-gated branch (persona
    storage needs a real database engine), which unit tests can't
    exercise without an ``aiosqlite`` test dependency this repo doesn't
    carry. A static AST check on the actual constructor call is the
    same technique already used by
    ``test_cloud_mode_static_guard.py`` for container wiring regressions
    that unit-level ``build_container()`` can't reach."""
    import ast
    import pathlib

    container_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src" / "kokoro_link" / "bootstrap" / "container.py"
    )
    tree = ast.parse(container_path.read_text(encoding="utf-8"))

    call_node = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "OperatorPersonaProjectionService"
        ):
            call_node = node
            break

    assert call_node is not None, (
        "OperatorPersonaProjectionService(...) construction not found in "
        "container.py"
    )
    kwarg_names = {kw.arg for kw in call_node.keywords}
    assert "operator_profile_service" in kwarg_names, (
        "container.py must pass operator_profile_service= to "
        "OperatorPersonaProjectionService so persona-projection narrative "
        "follows the owning operator's primary_language"
    )
