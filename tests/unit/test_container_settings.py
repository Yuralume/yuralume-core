import pytest

from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import (
    AppSettings,
    CloudSettings,
    MediaApiSettings,
    ObjectStorageSettings,
    PromptQualitySettings,
    UserTimezoneSettings,
)
from kokoro_link.application.services.cloud_active_llm_provider import (
    CloudActiveLLMProvider,
)
from kokoro_link.application.services.output_quality import OUTCOME_PASS
from kokoro_link.contracts.novelty_gate import NoveltyGateContext
from kokoro_link.infrastructure.cloud.official_card_exclusive_client import (
    EXCLUSIVE_READ_SCOPE,
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
from kokoro_link.infrastructure.schedule.llm_weather_drift import (
    LLMScheduleWeatherDriftJudge,
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


def _cloud_settings() -> AppSettings:
    return AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
        ),
    )


def test_container_feed_video_enabled_follows_local_registry_in_self_host(
    monkeypatch,  # noqa: ANN001
) -> None:
    """Self-host red line: outside cloud mode, ``video_enabled`` must keep
    following the local video-profile registry exactly as before — no local
    video config still means no video option offered to the model, and the
    hosted jobs knob has no say in it either."""
    monkeypatch.setenv("KOKORO_VIDEO_JOBS_ENABLED", "true")
    without_video = build_container(AppSettings(database_url=""))
    assert (
        without_video.feed_composer_service._composer._video_enabled  # noqa: SLF001
        is False
    )

    with_video = build_container(
        AppSettings(
            database_url="",
            video_api=MediaApiSettings(
                base_url="https://video.example",
                api_key="k",
                model="wan2.2",
            ),
        ),
    )
    assert with_video.video_profile_registry.profile_ids != []
    assert (
        with_video.feed_composer_service._composer._video_enabled  # noqa: SLF001
        is True
    )


def test_cloud_without_the_jobs_knob_offers_the_model_no_video_option(
    monkeypatch,  # noqa: ANN001
) -> None:
    """CV0-1, corrected: "a cloud video provider is wired" is not the same
    question as "video can be produced here".

    With the knob off the very same adapter only speaks the *synchronous*
    ``/v1/videos/generations`` route — one await of up to 30 minutes that
    holds the ``image`` capability slot for its whole duration. Offering
    ``media_kind=video`` there does not enable a feature, it lets a single
    post wedge the deployment's background media lane. Default-off must
    therefore be byte-for-byte the pre-deployment behaviour: no video
    option in the composer prompt at all.
    """
    monkeypatch.delenv("KOKORO_VIDEO_JOBS_ENABLED", raising=False)

    container = build_container(_cloud_settings())

    assert container.video_profile_registry.profile_ids == []
    composer = container.feed_composer_service._composer  # noqa: SLF001
    assert composer._video_enabled is False  # noqa: SLF001


def test_cloud_with_the_jobs_knob_enables_feed_video_without_local_video_env(
    monkeypatch,  # noqa: ANN001
) -> None:
    """The original CV0-1 bug, still fixed: hosted never configures
    ``KOKORO_VIDEO_*`` (video routes through the Gateway), so the local
    profile registry is empty and must not be what the flag reads. Once the
    deferred pipeline is switched on, the model is offered the option."""
    monkeypatch.setenv("KOKORO_VIDEO_JOBS_ENABLED", "true")

    container = build_container(_cloud_settings())

    assert container.video_profile_registry.profile_ids == []
    composer = container.feed_composer_service._composer  # noqa: SLF001
    assert composer._video_enabled is True  # noqa: SLF001


def test_only_a_deployment_that_can_queue_renders_probes_for_pending_rows(
    monkeypatch,  # noqa: ANN001
) -> None:
    """The composer's in-flight dedup probe is wired off the same condition
    as the poll carriers, so a deployment that can never hold a pending row
    never queries for one."""
    monkeypatch.delenv("KOKORO_VIDEO_JOBS_ENABLED", raising=False)
    self_host = build_container(AppSettings(database_url=""))
    assert (
        self_host.feed_composer_service._deferred_video_possible  # noqa: SLF001
        is False
    )

    cloud_without_knob = build_container(_cloud_settings())
    assert (
        cloud_without_knob.feed_composer_service._deferred_video_possible  # noqa: SLF001
        is False
    )

    monkeypatch.setenv("KOKORO_VIDEO_JOBS_ENABLED", "true")
    cloud_with_knob = build_container(_cloud_settings())
    assert (
        cloud_with_knob.feed_composer_service._deferred_video_possible  # noqa: SLF001
        is True
    )


def test_self_host_gets_no_video_poll_carrier(monkeypatch) -> None:  # noqa: ANN001
    """CV4 self-host red line, at the wiring level.

    The deferred pipeline object is always built (it is inert without an
    async-capable provider), but the embedded *carrier* must not be: a
    self-host tick may not gain even one query for rows it can never have.
    """
    monkeypatch.delenv("KOKORO_VIDEO_JOBS_ENABLED", raising=False)
    container = build_container(AppSettings(database_url=""))

    assert container.feed_video_job_service is not None
    scheduler = container.proactive_scheduler
    assert scheduler is not None
    assert scheduler._feed_video_job_service is None  # noqa: SLF001


def test_cloud_with_jobs_enabled_wires_the_embedded_poll_carrier(
    monkeypatch,  # noqa: ANN001
) -> None:
    """The knob is what turns LumeGram video into the deferred pipeline: it
    both flips the adapter's declared capability and wires the carrier that
    finishes the posts."""
    monkeypatch.setenv("KOKORO_VIDEO_JOBS_ENABLED", "true")
    container = build_container(AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
        ),
    ))

    scheduler = container.proactive_scheduler
    assert scheduler is not None
    assert scheduler._feed_video_job_service is not None  # noqa: SLF001
    assert (
        scheduler._feed_video_job_service  # noqa: SLF001
        is container.feed_video_job_service
    )


def test_cloud_without_the_knob_keeps_the_synchronous_video_route(
    monkeypatch,  # noqa: ANN001
) -> None:
    """Default-off: the media-jobs broker has to be deployed before Core may
    start queueing renders, and until then the same adapter must keep using
    the synchronous ``/v1/videos/generations`` route."""
    monkeypatch.delenv("KOKORO_VIDEO_JOBS_ENABLED", raising=False)
    container = build_container(AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
        ),
    ))

    scheduler = container.proactive_scheduler
    assert scheduler is not None
    assert scheduler._feed_video_job_service is None  # noqa: SLF001


def test_container_disables_tts_pregeneration_in_cloud_mode() -> None:
    """TS4: cloud mode charges TTS only when the player presses play.

    Background pregeneration would call the paid upstream provider before
    any button press (nobody to charge) and would let a later play request
    resolve from cache instead of the metered synthesize call. Cloud mode
    must never wire the pregeneration service at all — not just leave its
    preference permanently disabled — so neither call site in
    ``ChatService`` can invoke it.
    """
    settings = AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
        ),
    )

    container = build_container(settings)

    assert container.tts_pregeneration_service is None
    assert container.chat_service._tts_pregenerator is None  # noqa: SLF001


def test_container_wires_tts_pregeneration_service_in_self_host() -> None:
    """Self-host red line: TS4 must not change self-host behavior at all."""
    container = build_container(AppSettings(database_url=""))

    assert container.tts_pregeneration_service is not None
    assert (
        container.chat_service._tts_pregenerator  # noqa: SLF001
        is container.tts_pregeneration_service
    )


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
    assert container.character_ttl_reaper is not None
    assert (
        container.proactive_scheduler._character_ttl_reaper  # noqa: SLF001
        is container.character_ttl_reaper
    )


def test_cloud_container_wires_the_control_plane_tier_profile_port() -> None:
    """Every hosted tier's limits now come from the control-plane.

    Core carries no hardcoded tier->knob table since the demo profile was
    removed, so an unwired ``tier_profile_port`` no longer degrades one tier —
    it makes *every* hosted tier resolve to the permissive default, silently
    and expensively. Nothing else fails when that wire is dropped, which is
    exactly why both halves of the condition are pinned here.
    """
    container = build_container(
        AppSettings(
            cloud=CloudSettings(
                enabled=True,
                user_service_url="https://users.example",
                gateway_url="https://gateway.example",
                deployment_token="ykl_deploy",
                runtime_config_enabled=True,
            ),
        ),
    )

    # Reached through a consumer because the resolver is not itself a
    # container attribute; the reaper holds the very instance every other
    # consumer got.
    reaper = container.character_ttl_reaper
    assert reaper is not None
    resolver = reaper._account_runtime_profile_resolver  # noqa: SLF001
    assert resolver._tier_profile_port is not None  # noqa: SLF001


def test_cloud_container_leaves_the_tier_port_unwired_without_runtime_config() -> None:
    """The other half of the contract: runtime-config off is a supported
    deployment, and it resolves every tier to the permissive default rather
    than failing. Pinned so the port's gate cannot quietly widen."""
    container = build_container(_cloud_settings())

    reaper = container.character_ttl_reaper
    assert reaper is not None
    resolver = reaper._account_runtime_profile_resolver  # noqa: SLF001
    assert resolver._tier_profile_port is None  # noqa: SLF001


def test_container_wires_schedule_weather_drift_into_the_tick() -> None:
    container = build_container(AppSettings(database_url=""))

    service = container.schedule_weather_drift_service
    assert service is not None
    # The vet needs the same weather / operator sources the planner reads,
    # otherwise it would compare the day against a different sky.
    assert (
        service._weather_context_port  # noqa: SLF001
        is container.schedule_service._weather_context_port  # noqa: SLF001
    )
    assert (
        service._operator_profile_service  # noqa: SLF001
        is container.operator_profile_service
    )
    assert container.proactive_scheduler is not None
    assert (
        container.proactive_scheduler._schedule_weather_drift  # noqa: SLF001
        is service
    )
    assert (
        container.character_tick_executor._schedule_weather_drift  # noqa: SLF001
        is service
    )


def test_container_wires_llm_weather_drift_judge_regardless_of_boot_provider() -> None:
    """Providers are DB-backed runtime settings registered *after* the
    container is built, so ``default_provider_id`` at boot says nothing
    about whether a judge is reachable. The LLM judge's own per-call
    ``is_fake`` guard (which returns ``()``, the Null judge's answer) is
    what keeps a genuinely judge-less deployment from paying for a
    verdict — the old static check silently disabled the judge on
    self-hosts whose providers live only in the DB."""
    for settings in (
        AppSettings(database_url=""),
        AppSettings(database_url="", default_provider_id="lmstudio"),
    ):
        container = build_container(settings)

        assert isinstance(
            container.schedule_weather_drift_service._drift_port,  # noqa: SLF001
            LLMScheduleWeatherDriftJudge,
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


def test_container_uses_null_material_digester_only_when_disabled() -> None:
    """The operator switch is the only bootstrap decision. Whether a real
    model is reachable is per-call and DB-backed — the LLM digester's
    ``is_fake`` path returns ``None`` exactly like the Null one, so wiring
    it on a fake-boot deployment costs nothing."""
    disabled = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(material_digest_enabled=False),
        ),
    )
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
        LLMPromptMaterialDigester,
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


def test_container_uses_null_novelty_gate_only_when_disabled() -> None:
    """Enabled means the LLM gate, even on a fake boot provider: providers
    are DB-backed and land after container build, so "is there a judge" is
    the gate's per-call ``is_fake`` answer (``pass_unrouted``), not a
    bootstrap fact. The old static check silently turned the whole quality
    band off on self-hosts whose providers live only in the DB."""
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
        LLMNoveltyGate,
    )


def test_container_leaves_the_output_quality_orchestrator_ungated_when_off(
) -> None:
    """FC2 — a Null gate is *not* a gate, and the scrape must say so.

    ``NullNoveltyGate`` passes everything without a model call, so handing
    it to the orchestrator made every message record a ``pass`` on a
    deployment whose gate is switched off. AC3 is the opposite: a gate that
    is not wired renders no indicators. The services keep the Null instance
    — their own guard conditions are ``is None`` tests and would change
    behaviour — so the substitution happens only at the orchestrator's own
    parameter. The no-judge-route case is no longer decided here: it is the
    LLM gate's per-call ``pass_unrouted`` answer, which the orchestrator
    keeps off the scrape the same way (pinned below).
    """
    off = build_container(
        AppSettings(
            database_url="",
            default_provider_id="lmstudio",
            prompt_quality=PromptQualitySettings(novelty_gate_enabled=False),
        ),
    )
    assert off.output_quality_orchestrator.gate is None
    assert isinstance(
        off.chat_service._novelty_gate,  # noqa: SLF001
        NullNoveltyGate,
    )

    on = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(novelty_gate_enabled=True),
        ),
    )
    assert (
        on.output_quality_orchestrator.gate
        is on.chat_service._novelty_gate  # noqa: SLF001
    )
    assert isinstance(on.output_quality_orchestrator.gate, LLMNoveltyGate)


@pytest.mark.asyncio
async def test_unrouted_container_review_passes_without_counting_anything() -> None:
    """The counter half of the line above, now via the dynamic path: the
    gate is wired, its per-call resolution lands on the fake provider, and
    the review still records **nothing**.

    A ``pass`` recorded here is worse than no number, because it is
    indistinguishable from a deployment whose judge is reviewing every
    message and liking all of them.
    """
    container = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(novelty_gate_enabled=True),
        ),
    )

    review = await container.output_quality_orchestrator.review(
        "今天過得還可以",
        surface="promise",
        context_for=lambda candidate: NoveltyGateContext(
            character_id="c1", operator_id="op-1", response_text=candidate,
        ),
    )

    assert review.final == "今天過得還可以"
    assert review.outcome == OUTCOME_PASS
    assert container.output_quality_counters.snapshot() == {}


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


def test_container_wires_one_output_quality_orchestrator_into_every_surface() -> None:
    """QG0's whole point: the wiring lands **once**, here, so the six
    wave-3 tickets that adopt the orchestrator change only their own
    service file. A surface missing from this list is a surface whose
    ticket would have to reopen ``container.py`` and race the others.
    """
    container = build_container(AppSettings(database_url=""))

    orchestrator = container.output_quality_orchestrator
    assert orchestrator is not None
    # One instance, therefore one set of counters: the hard-skip rate is a
    # number about the deployment, not about whichever seam is asking.
    assert orchestrator.counters is container.output_quality_counters

    injected = [
        container.chat_service,
        container.proactive_dispatcher,
        container.feed_composer_service,
        container.feed_comment_reply_service,
        container.story_scene_service,
        container.character_encounter_service._runner,  # noqa: SLF001
    ]
    for service in injected:
        assert (
            service._output_quality_orchestrator  # noqa: SLF001
            is orchestrator
        ), type(service).__name__
    # The 起幕 wrap-up is reached through the scene service rather than the
    # container, and must not be left holding a different policy than the
    # opening it closes.
    assert (
        container.story_scene_service._closing  # noqa: SLF001
        ._output_quality_orchestrator is orchestrator
    )


def test_container_wires_the_encounter_runner_gate_flags() -> None:
    """Encounter used to take the gate port and none of its knobs, so a
    deployment that turned the gate off still ran it here. Both settings
    now reach it."""
    off = build_container(
        AppSettings(
            database_url="",
            default_provider_id="lmstudio",
            prompt_quality=PromptQualitySettings(
                novelty_gate_enabled=False, novelty_gate_max_retries=3,
            ),
        ),
    )
    on = build_container(
        AppSettings(
            database_url="",
            default_provider_id="lmstudio",
            prompt_quality=PromptQualitySettings(
                novelty_gate_enabled=True, novelty_gate_max_retries=3,
            ),
        ),
    )

    assert off.character_encounter_service._runner._novelty_gate is None  # noqa: SLF001
    assert isinstance(
        on.character_encounter_service._runner._novelty_gate,  # noqa: SLF001
        LLMNoveltyGate,
    )
    assert (
        on.character_encounter_service._runner._novelty_gate_max_retries == 3  # noqa: SLF001
    )


def test_container_wires_the_story_scene_gate_flags() -> None:
    """QG7b: 起幕 used to take the orchestrator and none of its knobs, so a
    deployment that turned the gate off (or raised its retry budget) still
    ran the opening and the wrap-up under QG7's hardcoded
    ``enabled=True, max_retries=1``. Both settings now reach both ends —
    the opening on the service itself and the wrap-up on the closing
    coordinator it hands the same values down to."""
    off = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(
                novelty_gate_enabled=False, novelty_gate_max_retries=3,
            ),
        ),
    )
    on = build_container(
        AppSettings(
            database_url="",
            prompt_quality=PromptQualitySettings(
                novelty_gate_enabled=True, novelty_gate_max_retries=3,
            ),
        ),
    )

    assert off.story_scene_service._reply_quality_gate_enabled is False  # noqa: SLF001
    assert on.story_scene_service._reply_quality_gate_enabled is True  # noqa: SLF001
    assert on.story_scene_service._reply_quality_gate_max_retries == 3  # noqa: SLF001
    assert (
        off.story_scene_service._closing  # noqa: SLF001
        ._reply_quality_gate_enabled is False
    )
    assert (
        on.story_scene_service._closing  # noqa: SLF001
        ._reply_quality_gate_enabled is True
    )
    assert (
        on.story_scene_service._closing  # noqa: SLF001
        ._reply_quality_gate_max_retries == 3
    )


def test_container_uses_null_register_profiler_only_when_disabled() -> None:
    """Flag-only selection — the LLM profiler's per-call ``is_fake`` path
    returns ``None`` exactly like the Null one, and DB-backed providers
    arrive after bootstrap, so a fake boot provider must not disable it."""
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
        LLMRegisterProfiler,
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


def test_schedule_plan_claim_ttl_outlives_one_slow_planner_round() -> None:
    """The plan claim must not expire while its own ``plan_day`` is still running.

    ``RuntimeClaim`` defaults to 180s and the schedule claim carries no
    heartbeat, but one planner round is a dialogue summary plus a ``plan_day``
    call and the LLM read timeout alone is 300s per call. At the default TTL a
    slow model let the claim lapse mid-plan, a second replica took the day over,
    and the unique constraint quietly absorbed the duplicate — correct data, but
    the spend was doubled and the player watched the plan flip on refresh.
    """
    container = build_container(AppSettings())

    claim = container.schedule_service._plan_claim
    assert claim is not None
    assert claim.ttl_seconds >= 600


def test_gated_catalog_client_absolutises_through_the_anonymous_catalog_client() -> None:
    """TG3 wiring red line, pinned at the container rather than re-derived in
    a hand-built unit test: the gated (tier-fenced) catalog client's asset
    absolutiser must be the *same bound method* the anonymous
    ``OfficialCardCatalogClient`` (wrapped by ``CachedOfficialCardCatalog``)
    uses for the public shelf — never one re-derived from
    ``cloud.user_service_url``.

    That second client lives on an internal host; absolutising a gated
    row's image path against it produces a URL the anonymous download
    guard rejects, and the card installs with no stage images and no
    error anywhere (see the wiring comment above
    ``gated_catalog_client = build_gated_catalog_client(...)`` in
    ``container.py``). A test that only reconstructs the two clients by
    hand and checks they *can* share an absolutiser (as
    ``test_official_card_gated_catalog_client.py`` does) would stay green
    if a future edit swapped in a differently-sourced client here — this
    test reads the actual production wiring instead.
    """
    settings = AppSettings(
        database_url="",
        cloud=CloudSettings(
            user_service_url="https://users.example",
            internal_service_credential=(
                f"key-1|core|yuralume-user|{EXCLUSIVE_READ_SCOPE}|s3cr3t"
            ),
        ),
    )

    container = build_container(settings)

    pack_source = container.character_card_pack_service._official_cards  # noqa: SLF001
    assert pack_source is not None
    gated_client = pack_source._gated_catalog  # noqa: SLF001
    assert gated_client is not None

    anonymous_client = pack_source._catalog._client  # noqa: SLF001
    assert gated_client._absolutise_asset.__self__ is anonymous_client  # noqa: SLF001
