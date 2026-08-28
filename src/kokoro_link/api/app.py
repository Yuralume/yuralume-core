import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from kokoro_link.api.dependencies import get_current_user, is_cloud_mode
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
from kokoro_link.api.routes.background_jobs_admin import (
    router as background_jobs_admin_router,
)
from kokoro_link.api.routes.execution_mode_admin import (
    router as execution_mode_admin_router,
)
from kokoro_link.api.routes.internal_video_trigger import (
    router as internal_video_trigger_router,
)
from kokoro_link.api.routes.character_relationships import (
    router as character_relationships_router,
)
from kokoro_link.api.routes.characters import router as character_router
from kokoro_link.api.routes.character_backups import (
    router as character_backups_router,
)
from kokoro_link.api.routes.character_cards import router as character_cards_router
from kokoro_link.api.routes.chat_assist import router as chat_assist_router
from kokoro_link.api.routes.chat import router as chat_router
from kokoro_link.api.routes.auth_locale import router as auth_locale_router
from kokoro_link.api.routes.cloud_credits import router as cloud_credits_router
from kokoro_link.api.routes.cloud_limits import router as cloud_limits_router
from kokoro_link.api.routes.cloud_pricing import router as cloud_pricing_router
from kokoro_link.api.routes.cloud_announcements import (
    router as cloud_announcements_router,
)
from kokoro_link.api.routes.geo import router as geo_router
from kokoro_link.api.routes.events import router as events_router
from kokoro_link.api.routes.feed import router as feed_router
from kokoro_link.api.routes.fusion_story import router as fusion_story_router
from kokoro_link.api.routes.studio_jobs import router as studio_jobs_router
from kokoro_link.api.routes.studio_material import (
    router as studio_material_router,
)
from kokoro_link.api.routes.goals import router as goal_router
from kokoro_link.api.routes.health import router as health_router
from kokoro_link.api.routes.external_chat import (
    register_external_chat_exception_handlers,
    router as external_chat_router,
)
from kokoro_link.api.routes.internal_cloud import (
    router as internal_cloud_router,
)
from kokoro_link.api.routes.internal_cloud_official_cards import (
    router as internal_cloud_official_cards_router,
)
from kokoro_link.api.routes.internal_cloud_showcase import (
    router as internal_cloud_showcase_router,
)
from kokoro_link.api.routes.internal_cloud_stats import (
    router as internal_cloud_stats_router,
)
from kokoro_link.api.routes.line_reactivation import (
    router as line_reactivation_router,
)
from kokoro_link.api.routes.internal_drain import (
    router as internal_drain_router,
)
from kokoro_link.api.routes.internal_metrics import (
    router as internal_metrics_router,
)
from kokoro_link.api.routes.memoir import router as memoir_router
from kokoro_link.api.routes.memory import router as memory_admin_router
from kokoro_link.api.routes.memory_consolidation import router as memory_router
from kokoro_link.api.routes.messaging import router as messaging_router
from kokoro_link.api.routes.nsfw_mode import router as nsfw_mode_router
from kokoro_link.api.routes.experiments import router as experiments_router
from kokoro_link.api.routes.observability import router as observability_router
from kokoro_link.api.routes.operator import overage_router
from kokoro_link.api.routes.operator import router as operator_router
from kokoro_link.api.routes.operator_persona import (
    router as operator_persona_router,
)
from kokoro_link.api.routes.pending_follow_ups import (
    router as pending_follow_ups_router,
)
from kokoro_link.api.routes.player_persona_note import (
    router as player_persona_note_router,
)
from kokoro_link.api.routes.player_identity_card import (
    router as player_identity_card_router,
)
from kokoro_link.api.routes.relationship_names import (
    router as relationship_names_router,
)
from kokoro_link.api.routes.initial_relationship import (
    router as initial_relationship_router,
)
from kokoro_link.api.routes.proactive import router as proactive_router
from kokoro_link.api.routes.push import router as push_router
from kokoro_link.api.routes.public_objects import router as public_objects_router
from kokoro_link.api.routes.schedule import router as schedule_router
from kokoro_link.api.routes.system import router as system_router
from kokoro_link.api.routes.story import router as story_router
from kokoro_link.api.routes.story_arc import router as story_arc_router
from kokoro_link.api.routes.story_scene import router as story_scene_router
from kokoro_link.api.routes.tools import router as tools_router
from kokoro_link.api.routes.tts import router as tts_router
from kokoro_link.api.routes.ui import router as ui_router
from kokoro_link.api.routes.usage import router as usage_router
from kokoro_link.api.routes.version import router as version_router
from kokoro_link.api.routes.world_events import router as world_events_router
from kokoro_link.application.services.chat_stream_relay import (
    wait_for_pending_turn_completions,
)
from kokoro_link.application.services.drain_state import (
    SERVER_DRAINING_CODE,
    ServerDrainingError,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessLocked,
)
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.process_roles import matrix_for_role
from kokoro_link.bootstrap.runtime_config_wiring import (
    build_runtime_config_refresher,
)
from kokoro_link.bootstrap.site_settings_refresh import (
    build_site_settings_refresher,
)
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.bootstrap.startup_seed_lock import startup_seed_lock
from kokoro_link.bootstrap.startup_seeds import run_startup_seeds
from kokoro_link.infrastructure.provider_settings.runtime_sync import (
    sync_provider_connections,
)
from kokoro_link.infrastructure.build_info import get_build_info
from kokoro_link.infrastructure.prompts import get_default_loader

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
DIST_DIR = FRONTEND_DIR / "dist"
LEGACY_STATIC_DIR = FRONTEND_DIR / "static"
_LOGGER = logging.getLogger(__name__)
# F2 — bound on how long lifespan shutdown waits for ChatService's
# fire-and-forget background work (post-turn outcome-claim audits, etc.)
# before abandoning it. A module attribute rather than a literal inline so
# tests can shrink it instead of a shutdown test taking 10 real seconds.
CHAT_BACKGROUND_DRAIN_TIMEOUT_SECONDS = 10.0


def _configure_logging() -> None:
    # Third-party request-line logs can contain credentials embedded in URLs
    # (Telegram Bot API tokens are part of the path).  Apply these floors even
    # when Uvicorn has already installed root handlers and we skip basicConfig.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
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


def create_app(settings: AppSettings | None = None) -> FastAPI:
    _configure_logging()
    settings = settings or AppSettings.from_env()
    build_info = get_build_info()
    _log_prompt_pack_overlay_status()
    container = build_container(settings)
    # Process-role component matrix (HOSTED_CORE_SCALING §2.1). Drives which
    # routers are registered below and which schedulers/connectors the
    # lifespan starts. Default role = all → historical single-container
    # behaviour.
    matrix = matrix_for_role(settings.process.role)

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
        # P2-B shadow runtime (HOSTED_CORE_SCALING §13 Phase 2). Both are None
        # unless YURALUME_BACKGROUND_SHADOW=postgres on a scheduler-owning role.
        shadow_coordinator = container.background_shadow_coordinator
        shadow_worker = container.background_shadow_worker
        # Phase 4 realtime outbox dispatcher (§7.1). Set only on the api reader
        # role under YURALUME_REALTIME_BACKEND=postgres; ``None`` everywhere else
        # (memory default / background writer / bare container), so the start /
        # stop below is a natural no-op off the postgres backend.
        realtime_dispatcher = getattr(container, "realtime_dispatcher", None)

        # Boot-time seed/sync batch, serialised across processes by a
        # PostgreSQL advisory lock. Seven Hosted processes come up at once and
        # every one of them runs these unlocked read-then-write upserts against
        # the same database; the lock turns that into a queue. Timing out means
        # we seed anyway (exactly the pre-lock behaviour), never that a
        # deployment silently skipped its seeds. Non-PostgreSQL takes no lock.
        async with startup_seed_lock(container.db_engine) as seed_lock_held:
            if not seed_lock_held:
                _LOGGER.debug(
                    "startup seeds running without the advisory lock "
                    "(single-process dialect or lock wait timed out)",
                )
            await run_startup_seeds(container, settings)

        # Cross-process runtime-config refresher (provider hot-reload). Built on
        # PostgreSQL only, in EVERY role: a coordinator/worker/connector serves
        # no admin route, so before this it could never learn that an operator
        # disabled a leaked key and kept using it until restarted. ``None`` on
        # SQLite / no database, where one process already syncs itself inline.
        runtime_config_refresher = build_runtime_config_refresher(
            engine=container.db_engine,
            database_url=settings.database_url,
            refresh=lambda: sync_provider_connections(container),
            poll_interval=settings.process.runtime_config_poll_interval,
        )
        if runtime_config_refresher is not None:
            await runtime_config_refresher.start()

        # Same discipline for the second hot config surface: the "real world"
        # site settings (weather coordinates / calendar region / GeoIP /
        # world-event policy). Before this, ``build_container`` read them once
        # and every process kept its own boot snapshot, so an Admin change took
        # effect on whichever replica served the request and needed a rolling
        # restart to reach the rest (G0 / plan §0.2 G-2). Built in EVERY role
        # for the same reason: a coordinator/worker composes prompts with these
        # facts and serves no admin route.
        site_settings_refresher = build_site_settings_refresher(
            engine=container.db_engine,
            database_url=settings.database_url,
            reload=getattr(container, "site_settings_reloader", None),
            poll_interval=settings.process.runtime_config_poll_interval,
        )
        if site_settings_refresher is not None:
            await site_settings_refresher.start()

        # Creator Studio durable jobs (C0): re-drive fusion/branching
        # pipelines the previous shutdown interrupted, so no story or
        # drama stays stuck on a non-terminal status with nothing
        # driving it. Fail-soft like every other startup step.
        studio_job_recovery = getattr(
            container, "studio_job_recovery_service", None,
        )
        # Studio executes as asyncio tasks in *this* API process (process-local
        # locks), so recovery must run only where the executor lives — the api
        # / all roles, never background (matrix.run_studio_recovery).
        if matrix.run_studio_recovery and studio_job_recovery is not None:
            try:
                report = await studio_job_recovery.recover()
                if any(report.values()):
                    print(
                        "[lifespan] studio job recovery "
                        f"resumed={report.get('resumed', 0)} "
                        f"finalized={report.get('finalized', 0)} "
                        f"failed={report.get('failed', 0)} "
                        f"superseded={report.get('superseded', 0)} "
                        f"pruned={report.get('pruned', 0)} "
                        f"lease_skipped={report.get('lease_skipped', 0)}"
                    )
            except Exception as exc:  # fail-soft
                print(f"[lifespan] studio job recovery failed: {exc!r}")

        # Character backup exports (CB2) execute exactly like studio jobs —
        # asyncio tasks in *this* API process — so their recovery rides the
        # same role gate: re-drive interrupted exports, prune old rows, and
        # run the key-material TTL backstop. Fail-soft like everything else.
        backup_export_service = getattr(
            container, "character_backup_export_service", None,
        )
        if matrix.run_studio_recovery and backup_export_service is not None:
            try:
                report = await backup_export_service.recover()
                if any(report.values()):
                    print(
                        "[lifespan] backup export recovery "
                        f"resumed={report.get('resumed', 0)} "
                        f"failed={report.get('failed', 0)} "
                        f"pruned={report.get('pruned', 0)} "
                        f"scrubbed={report.get('scrubbed', 0)} "
                        f"lease_skipped={report.get('lease_skipped', 0)}"
                    )
            except Exception as exc:  # fail-soft
                print(f"[lifespan] backup export recovery failed: {exc!r}")

        # Backup restores (CB3) mirror the exports: clean up whatever the
        # interrupted attempt half-landed, then rerun from scratch (or
        # fail terminally when attempts / key material / the staged
        # upload are gone). Runs AFTER the export recovery on purpose —
        # that one owns the shared prune + payload TTL backstop.
        backup_import_service = getattr(
            container, "character_backup_import_service", None,
        )
        if matrix.run_studio_recovery and backup_import_service is not None:
            try:
                report = await backup_import_service.recover()
                if any(report.values()):
                    print(
                        "[lifespan] backup restore recovery "
                        f"resumed={report.get('resumed', 0)} "
                        f"failed={report.get('failed', 0)} "
                        f"lease_skipped={report.get('lease_skipped', 0)}"
                    )
            except Exception as exc:  # fail-soft
                print(f"[lifespan] backup restore recovery failed: {exc!r}")

        # Role-gated startup: embedded schedulers and messaging connectors run
        # only where the matrix allows (HOSTED_CORE_SCALING §2.1). A dedicated
        # coordinator starts only the lease-gated world-event singleton below.
        # Shutdown mirrors exactly — only stop what started.
        if matrix.start_schedulers:
            if proactive is not None:
                await proactive.start()
            if world_event_scheduler is not None:
                await world_event_scheduler.start()
        # §2.1 dedicated roles: the durable coordinator/worker loops are gated
        # independently of the embedded scheduler. ``all`` / ``background`` set
        # both flags (they ride together, as before); the dedicated
        # ``worker`` starts exactly its queue loop; ``coordinator`` starts its
        # durable loop followed by the lease-gated world-event singleton.
        if matrix.run_background_coordinator and shadow_coordinator is not None:
            await shadow_coordinator.start()
        if matrix.start_world_event_scheduler and not matrix.start_schedulers:
            if shadow_coordinator is None:
                raise RuntimeError(
                    "dedicated world-event scheduler requires coordinator lease",
                )
            if world_event_scheduler is not None:
                await world_event_scheduler.start()
        if matrix.run_background_worker and shadow_worker is not None:
            await shadow_worker.start()
        if matrix.start_connectors:
            if telegram_polling is not None:
                await telegram_polling.start()
            if discord_gateway is not None:
                await discord_gateway.start()
            if whatsapp_gateway is not None:
                await whatsapp_gateway.start()
        # Realtime outbox dispatcher: independent of the scheduler/connector
        # gates (it runs on the api role, which starts neither) — the None guard
        # is the only gate needed. Priming its cursor at the current tail means a
        # fresh replica forwards only events appended after it came up.
        if realtime_dispatcher is not None:
            await realtime_dispatcher.start()
        try:
            yield
        finally:
            # Stop the LISTEN/poll-driven components first so their loops unwind
            # before the shared engine's pool is disposed below.
            if runtime_config_refresher is not None:
                try:
                    await runtime_config_refresher.stop()
                except Exception as exc:  # fail-soft: never mask a clean shutdown
                    print(
                        "[lifespan] runtime config refresher stop failed: "
                        f"{exc!r}"
                    )
            if site_settings_refresher is not None:
                try:
                    await site_settings_refresher.stop()
                except Exception as exc:  # fail-soft: never mask a clean shutdown
                    print(
                        "[lifespan] site settings refresher stop failed: "
                        f"{exc!r}"
                    )
            if realtime_dispatcher is not None:
                try:
                    await realtime_dispatcher.stop()
                except Exception as exc:  # fail-soft: never mask a clean shutdown
                    print(f"[lifespan] realtime dispatcher stop failed: {exc!r}")
            if matrix.start_connectors:
                if whatsapp_gateway is not None:
                    await whatsapp_gateway.stop()
                if discord_gateway is not None:
                    await discord_gateway.stop()
                if telegram_polling is not None:
                    await telegram_polling.stop()
            # Mirror startup order in reverse: durable worker/coordinator loops
            # first (each gated on the same flag it started under), then the
            # embedded schedulers.
            if matrix.run_background_worker and shadow_worker is not None:
                await shadow_worker.stop()
            if matrix.start_world_event_scheduler and not matrix.start_schedulers:
                if world_event_scheduler is not None:
                    await world_event_scheduler.stop()
            if matrix.run_background_coordinator and shadow_coordinator is not None:
                await shadow_coordinator.stop()
            if matrix.start_schedulers:
                if world_event_scheduler is not None:
                    await world_event_scheduler.stop()
                if proactive is not None:
                    await proactive.stop()
            # Detached chat turns (client disconnected, generation still
            # running — see ``chat_stream_relay``) first, and *before* the F2
            # drain below rather than after: finishing a turn is what schedules
            # the post-turn tails F2 waits for, so the other order would drain
            # an empty set and then abandon the tasks it was there to catch.
            #
            # Belt and braces, not the primary protection: GD's deploy protocol
            # polls ``active_turns`` to zero before the container is recreated,
            # and a detached turn is a counted turn. This only matters for a
            # shutdown that skipped that (self-host Ctrl-C, a crash-adjacent
            # restart), where it buys an almost-done turn the seconds it needs
            # to persist instead of dying at loop close.
            try:
                await asyncio.wait_for(
                    wait_for_pending_turn_completions(),
                    timeout=CHAT_BACKGROUND_DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                print(
                    "[lifespan] detached chat turns still running after "
                    f"{CHAT_BACKGROUND_DRAIN_TIMEOUT_SECONDS}s — abandoning them"
                )
            except Exception as exc:  # fail-soft: never mask a clean shutdown
                print(f"[lifespan] detached chat turn drain failed: {exc!r}")
            # F2 — drain ChatService's fire-and-forget background work
            # (post-turn outcome-claim audits, auto-consolidation, …)
            # before the shared engine's pool goes away below: several of
            # those tasks write through it. Bounded so a stuck judge call
            # can never hang shutdown forever; still-running tasks past the
            # deadline are abandoned (each one's own CancelledError
            # handling logs the loss — see chat_outcome_claim_auditor.py).
            try:
                await asyncio.wait_for(
                    container.chat_service.wait_for_pending(),
                    timeout=CHAT_BACKGROUND_DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                print(
                    "[lifespan] chat service background drain timed out "
                    f"after {CHAT_BACKGROUND_DRAIN_TIMEOUT_SECONDS}s — "
                    "abandoning still-pending tasks"
                )
            except Exception as exc:  # fail-soft: never mask a clean shutdown
                print(f"[lifespan] chat service background drain failed: {exc!r}")
            # HOSTED_CORE_SCALING §9.1 — dispose the single shared async
            # engine (releases its connection pool) once, in every process
            # role. Fail-soft so a dispose error never masks a clean
            # shutdown; ``None`` on in-memory builds is a no-op.
            if container.db_engine is not None:
                try:
                    await container.db_engine.dispose()
                except Exception as exc:  # fail-soft
                    print(f"[lifespan] db_engine dispose failed: {exc!r}")

    app = FastAPI(title="Yuralume", version=build_info.version, lifespan=lifespan)
    app.state.container = container
    app.state.settings = settings
    # Stash the process-role component matrix so the /health probe can be
    # scheduler-liveness-aware: a role that starts schedulers (all/background)
    # must 503 when its scheduler task has died, while api / bare-container /
    # never-started shapes stay 200 (see api/routes/health.py).
    app.state.matrix = matrix

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

    # GD1-A: a turn that starts on a draining replica is refused before it costs
    # the player anything. One handler rather than a per-route ``except`` so
    # every transport that drives a turn (web chat, external chat, messaging
    # webhooks) answers the same way — the refusal has to be uniform precisely
    # because it is the fallback for the case where the router did NOT stop
    # sending this replica traffic.
    @app.exception_handler(ServerDrainingError)
    async def _server_draining_handler(
        request: Request, exc: ServerDrainingError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": SERVER_DRAINING_CODE,
                    "message": str(exc),
                },
            },
            headers={"Retry-After": "1"},
        )

    # M11: external-chat internal routes answer credential-gate (401/503) and
    # request-validation (422) failures with the same ErrorEnvelope as their
    # other errors. The validation handler is a no-op for every non-external-chat
    # path (forwarded to FastAPI's default), so global behaviour is unchanged.
    register_external_chat_exception_handlers(app)

    # Health probe is served in every process role (background exposes only a
    # loopback health/metrics surface — no public API, SPA, SSE, or uploads).
    app.include_router(health_router)
    # Internal Prometheus scrape endpoint (§12 / Phase 0). Registered in every
    # role whose matrix sets ``serve_metrics_route`` — including background, so
    # the singleton scheduler's tick timings are observable — BEFORE the
    # background early return. Own per-channel bearer token (not the operator
    # JWT), kept off the /api/v1 operator surface.
    if matrix.serve_metrics_route:
        app.include_router(internal_metrics_router, prefix="/api/internal/v1")
        # GD1-A drain switch. Same gate, same prefix, same bearer channel as the
        # scrape above: both are this replica's loopback management surface and
        # the deploy script reaches them over the same port in the same step.
        app.include_router(internal_drain_router, prefix="/api/internal/v1")
    if not matrix.serve_api_routes:
        # background role: schedulers/connectors run headless; nothing else is
        # mounted, so a representative /api/v1 route 404s by construction.
        return app

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

    app.include_router(public_objects_router)
    app.include_router(version_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(character_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(character_backups_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(character_cards_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(character_relationships_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(chat_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(chat_assist_router, prefix="/api/v1", dependencies=_auth_dep)
    # Cloud-only player surfaces (U3 credit badge). Mounted unconditionally —
    # the handlers 404 outside cloud mode, matching /auth/cloud/session — so the
    # self-host route inventory is unchanged.
    app.include_router(cloud_credits_router, prefix="/api/v1", dependencies=_auth_dep)
    # AP3 price list + AP4 overage switches. Mounted *conditionally*, unlike the
    # older cloud routers above: those predate the rule and 404 in-handler, but
    # a route that exists only to 404 still shows up in a self-host install's
    # OpenAPI inventory. Hosted-only surfaces added from AP3 on are simply
    # absent there, so self-host's API surface is unchanged by this line.
    if is_cloud_mode(container):
        app.include_router(cloud_pricing_router, prefix="/api/v1", dependencies=_auth_dep)
        app.include_router(overage_router, prefix="/api/v1", dependencies=_auth_dep)
        # AN1 notice-board dot. Conditional for the same reason as the two
        # above: a self-host install has no Cloud board, so the route should be
        # absent from its inventory rather than present-and-404ing.
        app.include_router(
            cloud_announcements_router, prefix="/api/v1", dependencies=_auth_dep,
        )
        # Runtime-limit hints (character slots / daily creates / daily 起幕 /
        # session message cap / capability switches). Conditional for the same
        # reason: the ceilings only exist on hosted tiers, so self-host should
        # not carry the route at all. The handler still 404s outside cloud
        # mode, which is what a direct mount in a test exercises.
        app.include_router(
            cloud_limits_router, prefix="/api/v1", dependencies=_auth_dep,
        )
    # G2 hosted locale lifecycle + city search. Same unconditional mount /
    # in-handler 404: self-host's route inventory is unchanged, and both
    # already carry their own per-handler bearer dependency (the locale
    # router lives under the /auth prefix, which is exempt from the blanket
    # dependency for the public login endpoints' sake).
    app.include_router(auth_locale_router, prefix="/api/v1")
    app.include_router(geo_router, prefix="/api/v1", dependencies=_auth_dep)
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
    app.include_router(
        initial_relationship_router, prefix="/api/v1", dependencies=_auth_dep,
    )
    app.include_router(
        player_persona_note_router, prefix="/api/v1", dependencies=_auth_dep,
    )
    app.include_router(
        player_identity_card_router, prefix="/api/v1", dependencies=_auth_dep,
    )
    app.include_router(pending_follow_ups_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(proactive_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(push_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(system_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(nsfw_mode_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(admin_providers_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(admin_characters_router, prefix="/api/v1", dependencies=_auth_dep)
    # CV6 — admin-only full-chain video test
    # trigger. Not a player-facing surface and not on the public nginx
    # allowlist (an infra-side concern); admin auth on the route is the
    # whole gate. Registered unconditionally like the other admin-* routers
    # above — the service layer itself reports "no async pipeline here"
    # rather than requiring the route to know the deployment shape.
    app.include_router(
        internal_video_trigger_router, prefix="/api/v1", dependencies=_auth_dep,
    )
    # Background-jobs admin diagnostics (P2-B). Registered ONLY when the shadow
    # queue is on (``YURALUME_BACKGROUND_SHADOW=postgres``) so shadow-off
    # deployments keep the exact pre-Phase-2 surface (these paths 404, not a new
    # 200/503 degrade shape). With shadow on, M6 builds the queue port in every
    # api-serving role, so the routes return real data on api and all alike.
    if settings.process.background_shadow == "postgres":
        app.include_router(background_jobs_admin_router, prefix="/api/v1", dependencies=_auth_dep)
    # Execution-mode admin (P3-B, §11/§15). Registered when the ownership port
    # COULD be wired — either the shadow queue is on OR the process opted into
    # the postgres background backend — so a distributed rollout can flip the
    # mutual-exclusion mode. Off both → the paths 404/405 exactly like before.
    if (
        settings.process.background_shadow == "postgres"
        or settings.process.background_backend == "postgres"
    ):
        app.include_router(execution_mode_admin_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(tools_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(story_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(story_arc_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(story_scene_router, prefix="/api/v1", dependencies=_auth_dep)
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
    # Must come *after* observability_router, which owns the static
    # `/admin/app-settings/{quiet-hours,humanization-flags,
    # persona-curiosity-flags}` routes. Starlette matches in registration
    # order, so mounting this router first let its greedy
    # `/admin/app-settings/{group}` swallow all three and answer
    # `404 unknown group: quiet-hours` — the same shadowing trap the
    # arc-template intake router is ordered around above.
    app.include_router(admin_app_settings_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(usage_router, prefix="/api/v1", dependencies=_auth_dep)
    app.include_router(experiments_router, prefix="/api/v1", dependencies=_auth_dep)
    # Service-to-service Cloud→Core channel. Deliberately NOT behind
    # ``_auth_dep`` (operator JWT) — it authenticates with a shared internal
    # bearer token checked inside the router (``KOKORO_CLOUD_INTERNAL_TOKENS``,
    # fail-closed). Mounted under /api/internal/v1 to keep it off the
    # operator-facing /api/v1 surface.
    # Phase 4 (§7.1) retired the §7.0 internal realtime relay consumer: the
    # background→api bridge is now the durable PostgreSQL outbox tailed by the
    # api-side RealtimeEventDispatcher (started in the lifespan), so there is no
    # HTTP relay route to mount.
    if matrix.serve_cloud_internal_routes:
        app.include_router(internal_cloud_router, prefix="/api/internal/v1")
        app.include_router(internal_cloud_showcase_router, prefix="/api/internal/v1")
        app.include_router(internal_cloud_stats_router, prefix="/api/internal/v1")
        app.include_router(
            internal_cloud_official_cards_router, prefix="/api/internal/v1",
        )
        # LR series — the dormant-reactivation campaign channel. Mounted
        # beside its siblings; the route itself answers 503
        # ``cloud_mode_required`` when the subsystem is unwired, so a
        # deployment that serves internal routes without the hosted
        # proactive path says so rather than 404-ing.
        app.include_router(line_reactivation_router, prefix="/api/internal/v1")
        app.include_router(external_chat_router, prefix="/api/internal/v1")
    app.include_router(ui_router)
    return app
