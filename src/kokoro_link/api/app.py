import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.application.exceptions import CharacterNotOwned

from kokoro_link.api.routes.album import router as album_router
from kokoro_link.api.routes.branching_drama import router as branching_drama_router
from kokoro_link.api.routes.arc_template_intake import router as arc_template_intake_router
from kokoro_link.api.routes.arc_templates import router as arc_templates_router
from kokoro_link.api.routes.arc_series import router as arc_series_router
from kokoro_link.api.routes.auth import router as auth_router
from kokoro_link.api.routes.admin_providers import (
    router as admin_providers_router,
)
from kokoro_link.api.routes.admin_app_settings import (
    router as admin_app_settings_router,
)
from kokoro_link.api.routes.admin_characters import (
    router as admin_characters_router,
)
from kokoro_link.api.routes.character_relationships import (
    router as character_relationships_router,
)
from kokoro_link.api.routes.characters import router as character_router
from kokoro_link.api.routes.character_cards import router as character_cards_router
from kokoro_link.api.routes.chat_assist import router as chat_assist_router
from kokoro_link.api.routes.chat import router as chat_router
from kokoro_link.api.routes.events import router as events_router
from kokoro_link.api.routes.feed import router as feed_router
from kokoro_link.api.routes.fusion_story import router as fusion_story_router
from kokoro_link.api.routes.studio_jobs import router as studio_jobs_router
from kokoro_link.api.routes.studio_material import (
    router as studio_material_router,
)
from kokoro_link.api.routes.goals import router as goal_router
from kokoro_link.api.routes.health import router as health_router
from kokoro_link.api.routes.internal_cloud import (
    router as internal_cloud_router,
)
from kokoro_link.api.routes.memoir import router as memoir_router
from kokoro_link.api.routes.memory import router as memory_admin_router
from kokoro_link.api.routes.memory_consolidation import router as memory_router
from kokoro_link.api.routes.messaging import router as messaging_router
from kokoro_link.api.routes.nsfw_mode import router as nsfw_mode_router
from kokoro_link.api.routes.experiments import router as experiments_router
from kokoro_link.api.routes.observability import router as observability_router
from kokoro_link.api.routes.operator import router as operator_router
from kokoro_link.api.routes.operator_persona import (
    router as operator_persona_router,
)
from kokoro_link.api.routes.pending_follow_ups import (
    router as pending_follow_ups_router,
)
from kokoro_link.api.routes.relationship_names import (
    router as relationship_names_router,
)
from kokoro_link.api.routes.proactive import router as proactive_router
from kokoro_link.api.routes.push import router as push_router
from kokoro_link.api.routes.public_objects import router as public_objects_router
from kokoro_link.api.routes.schedule import router as schedule_router
from kokoro_link.api.routes.system import router as system_router
from kokoro_link.api.routes.story import router as story_router
from kokoro_link.api.routes.story_arc import router as story_arc_router
from kokoro_link.api.routes.tools import router as tools_router
from kokoro_link.api.routes.tts import router as tts_router
from kokoro_link.api.routes.ui import router as ui_router
from kokoro_link.api.routes.usage import router as usage_router
from kokoro_link.api.routes.version import router as version_router
from kokoro_link.api.routes.world_events import router as world_events_router
from kokoro_link.application.services.bootstrap_admin_seed import (
    seed_bootstrap_admin,
)
from kokoro_link.application.services.default_locale_seed import (
    seed_default_locale,
)
from kokoro_link.application.services.preference_validator import (
    ModelPreferenceValidator,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessLocked,
)
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.provider_settings.runtime_sync import (
    seed_legacy_provider_connections,
    sync_provider_connections,
)
from kokoro_link.infrastructure.build_info import get_build_info
from kokoro_link.infrastructure.prompts import get_default_loader

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
DIST_DIR = FRONTEND_DIR / "dist"
LEGACY_STATIC_DIR = FRONTEND_DIR / "static"
_LOGGER = logging.getLogger(__name__)


def _configure_logging() -> None:
    # Uvicorn's --log-level only touches uvicorn.* loggers. Without
    # configuring the root logger here, application _LOGGER.info(...)
    # calls are filtered by Python's WARNING default. Driven by
    # KOKORO_LOG_LEVEL so `make dev` can opt into INFO while a prod
    # entry can stay quiet.
    if logging.getLogger().handlers:
        return
    level_name = os.getenv("KOKORO_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _log_prompt_pack_overlay_status() -> None:
    status = get_default_loader().overlay_status()
    if not status.configured:
        _LOGGER.info(
            "Prompt pack overlay disabled; using bundled prompts "
            "effective_templates=%d",
            status.effective_template_count,
        )
        return

    if not status.is_dir:
        _LOGGER.warning(
            "Prompt pack overlay path is not a directory; path=%s exists=%s "
            "is_dir=%s overlay_templates=0 effective_templates=%d",
            status.path,
            status.exists,
            status.is_dir,
            status.effective_template_count,
        )
        return

    if status.overlay_template_count == 0:
        _LOGGER.warning(
            "Prompt pack overlay configured but empty; path=%s "
            "overlay_templates=0 effective_templates=%d",
            status.path,
            status.effective_template_count,
        )
        return

    _LOGGER.info(
        "Prompt pack overlay loaded; path=%s overlay_templates=%d "
        "effective_templates=%d sample=%s",
        status.path,
        status.overlay_template_count,
        status.effective_template_count,
        ",".join(status.sample_templates),
    )


def create_app() -> FastAPI:
    _configure_logging()
    settings = AppSettings.from_env()
    build_info = get_build_info()
    _log_prompt_pack_overlay_status()
    container = build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Background schedulers run as single asyncio tasks for the
        # app's lifetime. Unit tests that instantiate ``ServiceContainer``
        # directly won't start them.
        proactive = container.proactive_scheduler
        world_event_scheduler = container.world_event_scheduler
        telegram_polling = container.telegram_polling_service
        discord_gateway = container.discord_gateway_service
        whatsapp_gateway = container.whatsapp_gateway_service
        rss_source_sync = container.rss_source_sync_service

        # Reset any model preferences that point at providers / model
        # ids no longer registered (operator changed env, removed an
        # adapter, unloaded a model in LM Studio…). Without this the
        # DB pref keeps shadowing env and the only way back is editing
        # the picker by hand or running SQL.
        await seed_legacy_provider_connections(container, settings)
        await sync_provider_connections(container)

        # Site-level runtime settings (Weather/Calendar/GeoIP/NSFW/world-event
        # policy): first-boot seed env → app_runtime_settings when a group's
        # DB row is absent, then DB is authoritative. The Weather/Calendar
        # providers were already wired from the DB-overlaid settings at
        # container build; here we also seed so the Admin「站點設定」page has
        # rows to edit (CORE_ENV_TO_ADMIN_CONFIG track 2).
        if container.runtime_settings_repository is not None:
            from kokoro_link.application.services.app_runtime_settings_service import (
                AppRuntimeSettingsService,
            )
            from kokoro_link.bootstrap.app_runtime_settings_seed import (
                seed_app_runtime_settings,
            )
            await seed_app_runtime_settings(
                AppRuntimeSettingsService(container.runtime_settings_repository),
                settings,
            )

        # First-run admin bootstrap from BOOTSTRAP_ADMIN_* env vars.
        # No-op once the default user has credentials, so safe to leave
        # in long-running deployments. Independent of auth.enabled — a
        # deployment can pre-seed credentials before flipping the auth
        # switch on.
        if container.auth_service is not None:
            try:
                await seed_bootstrap_admin(
                    container.auth_service,
                    email=settings.auth.bootstrap_admin_email,
                    password=settings.auth.bootstrap_admin_password,
                )
            except Exception as exc:  # belt-and-braces; seed is itself fail-soft
                print(f"[lifespan] bootstrap_admin_seed failed: {exc!r}")

        # Deploy-time default UI/content language + timezone for the
        # single-user default operator (USER_PRIMARY_LANGUAGE / USER_TIMEZONE,
        # written by the self-host installer). Applies only while the default
        # row is unconfigured (no password); a real /auth/setup locks both.
        # Skipped in cloud mode, where identity + prefs are federated, not
        # seeded onto a local default row.
        operator_repo = getattr(container, "operator_profile_repository", None)
        if operator_repo is not None and not getattr(
            settings.cloud, "active", False,
        ):
            try:
                await seed_default_locale(
                    operator_repo,
                    language=settings.default_primary_language,
                    timezone_id=settings.user_timezone.default_timezone_id,
                )
            except Exception as exc:  # belt-and-braces; seed is itself fail-soft
                print(f"[lifespan] default_locale_seed failed: {exc!r}")

        pref_validator = ModelPreferenceValidator(
            registry=container.model_registry,
            preferences=container.preferences_repository,
            default_provider_id=settings.default_provider_id,
        )
        try:
            await pref_validator.repair()
        except Exception as exc:  # fail-soft: never block startup on this
            print(f"[lifespan] model preference repair failed: {exc!r}")

        if rss_source_sync is not None:
            try:
                touched = await rss_source_sync.sync()
                if touched:
                    print(f"[lifespan] rss_source_sync touched {touched} rows")
            except Exception as exc:  # fail-soft: missing yaml etc.
                print(f"[lifespan] rss_source_sync failed: {exc!r}")
            # First-boot bridge: deprecated KOKORO_WORLD_EVENT_FEED_* env →
            # rss_sources table (CORE_ENV_TO_ADMIN_CONFIG track 3). Only
            # inserts ids not already present, so admin deletions stick.
            try:
                from kokoro_link.application.services.rss_source_sync_service import (
                    EnvFeedSeed,
                )
                env_feeds = tuple(
                    EnvFeedSeed(
                        source_id=f.source_id,
                        url=f.url,
                        topic_tags=f.topic_tags,
                    )
                    for f in settings.world_events.feeds
                )
                seeded = await rss_source_sync.seed_env_feeds(env_feeds)
                if seeded:
                    print(f"[lifespan] world-event env feeds seeded {seeded} rows")
            except Exception as exc:  # fail-soft
                print(f"[lifespan] world-event env feed seed failed: {exc!r}")

        # Arc template pack sync — YAML files under
        # src/kokoro_link/data/arc_templates/ upserted as user_id=NULL
        # rows in the arc_templates table. Same fail-soft policy: a
        # crashed sync leaves whatever's in the DB intact rather than
        # blocking startup.
        arc_template_pack_sync = container.arc_template_pack_sync_service
        if arc_template_pack_sync is not None:
            try:
                touched = await arc_template_pack_sync.sync()
                if touched:
                    print(
                        f"[lifespan] arc_template_pack_sync upserted "
                        f"{touched} rows"
                    )
            except Exception as exc:  # fail-soft
                print(f"[lifespan] arc_template_pack_sync failed: {exc!r}")
        # Creator Studio durable jobs (C0): re-drive fusion/branching
        # pipelines the previous shutdown interrupted, so no story or
        # drama stays stuck on a non-terminal status with nothing
        # driving it. Fail-soft like every other startup step.
        studio_job_recovery = getattr(
            container, "studio_job_recovery_service", None,
        )
        if studio_job_recovery is not None:
            try:
                report = await studio_job_recovery.recover()
                if any(report.values()):
                    print(
                        "[lifespan] studio job recovery "
                        f"resumed={report.get('resumed', 0)} "
                        f"finalized={report.get('finalized', 0)} "
                        f"failed={report.get('failed', 0)} "
                        f"superseded={report.get('superseded', 0)} "
                        f"pruned={report.get('pruned', 0)}"
                    )
            except Exception as exc:  # fail-soft
                print(f"[lifespan] studio job recovery failed: {exc!r}")

        if proactive is not None:
            await proactive.start()
        if world_event_scheduler is not None:
            await world_event_scheduler.start()
        if telegram_polling is not None:
            await telegram_polling.start()
        if discord_gateway is not None:
            await discord_gateway.start()
        if whatsapp_gateway is not None:
            await whatsapp_gateway.start()
        try:
            yield
        finally:
            if whatsapp_gateway is not None:
                await whatsapp_gateway.stop()
            if discord_gateway is not None:
                await discord_gateway.stop()
            if telegram_polling is not None:
                await telegram_polling.stop()
            if world_event_scheduler is not None:
                await world_event_scheduler.stop()
            if proactive is not None:
                await proactive.stop()

    app = FastAPI(title="Yuralume", version=build_info.version, lifespan=lifespan)
    app.state.container = container
    app.state.settings = settings

    # CharacterNotOwned → 404 (deliberately same as "not found" to
    # prevent cross-user enumeration). Service layer raises this from
    # the ownership guard; without a handler FastAPI would 500.
    @app.exception_handler(CharacterNotOwned)
    async def _character_not_owned_handler(
        request: Request, exc: CharacterNotOwned,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"detail": "Character not found"},
        )

    @app.exception_handler(SubscriptionAccessLocked)
    async def _subscription_access_locked_handler(
        request: Request, exc: SubscriptionAccessLocked,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "detail": {
                    "code": "subscription_frozen",
                    "message": str(exc),
                },
            },
        )

    # Serve Vue build assets if available, otherwise legacy static
    assets_dir = DIST_DIR / "assets" if DIST_DIR.exists() else LEGACY_STATIC_DIR
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    # Legacy read-only compatibility for existing DB rows that still
    # contain `/uploads/...` URLs. New media writes go through Object
    # Storage and are exposed through the app's public `/v1/public/...`
    # route so self-host deployments can serve media under one domain.
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/uploads",
        StaticFiles(directory=settings.uploads_dir),
        name="uploads",
    )

    # Auth-aware router include helper. When KOKORO_AUTH_ENABLED=true
    # every API endpoint requires a bearer token; in disabled mode the
    # dependency short-circuits to the default user. Auth + health +
    # ui + static assets stay public (auth flow itself can't require
    # a token, /health is a probe, static files are CDN-style).
    _auth_dep = [Depends(get_current_user)]

    app.include_router(health_router)
    app.include_router(public_objects_router)
    app.include_router(version_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(character_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(character_cards_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(character_relationships_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(chat_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(chat_assist_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(events_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(goal_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(schedule_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(memory_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(memory_admin_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(memoir_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(messaging_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(operator_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(operator_persona_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(relationship_names_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(pending_follow_ups_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(proactive_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(push_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(system_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(nsfw_mode_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(admin_providers_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(admin_app_settings_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(admin_characters_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(tools_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(story_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(story_arc_router, prefix="/api/v1", dependencies=_auth_dep)
    # Intake router must come before arc_templates_router so that the
    # static `/arc-templates/scaffolds` and `/arc-templates/intake/...`
    # routes match before the greedy `/arc-templates/{template_id}` in
    # the read-only router (which would otherwise capture "scaffolds"
    # as a template id and return 404).
    app.include_router(arc_template_intake_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(arc_templates_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(arc_series_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(album_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(feed_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(tts_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(fusion_story_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(studio_jobs_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(studio_material_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(branching_drama_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(world_events_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(observability_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(usage_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(experiments_router, prefix="/api/v1", dependencies=_auth_dep)
    # Service-to-service Cloud→Core channel. Deliberately NOT behind
    # ``_auth_dep`` (operator JWT) — it authenticates with a shared internal
    # bearer token checked inside the router (``KOKORO_CLOUD_INTERNAL_TOKENS``,
    # fail-closed). Mounted under /api/internal/v1 to keep it off the
    # operator-facing /api/v1 surface.
    app.include_router(internal_cloud_router, prefix="/api/internal/v1")
    app.include_router(ui_router)
    return app
