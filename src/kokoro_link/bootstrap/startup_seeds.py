"""The lifespan startup seed batch, extracted from ``api/app.py``.

Everything here is a first-boot / every-boot convergence step that reads the
database and writes what is missing: the legacy provider-env seed, the
``app_runtime_settings`` group seed, the bootstrap admin credential, the default
operator locale, the model-preference repair, the RSS source + env-feed sync and
the arc-template pack sync.

Two reasons it lives in its own module rather than inline in the lifespan:

* it is the unit the startup advisory lock wraps (see
  :mod:`kokoro_link.bootstrap.startup_seed_lock`) — having one callable makes
  the locked region obvious instead of "these hundred lines of the lifespan";
* ``api/app.py`` is the router-registration surface, and a hundred lines of
  seeding buried in its lifespan is what let the multi-process race hide for as
  long as it did.

Every step keeps its original fail-soft policy verbatim: a seed that raises must
never stop the process from coming up, because a degraded-but-running deployment
is strictly better than a crash loop on a first-boot convenience.
"""

from __future__ import annotations

from kokoro_link.application.services.bootstrap_admin_seed import (
    seed_bootstrap_admin,
)
from kokoro_link.application.services.default_locale_seed import (
    seed_default_locale,
)
from kokoro_link.application.services.preference_validator import (
    ModelPreferenceValidator,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.provider_settings.runtime_sync import (
    seed_legacy_provider_connections,
    sync_provider_connections,
)


async def run_startup_seeds(
    container: ServiceContainer, settings: AppSettings,
) -> None:
    """Run every boot-time seed/sync. Safe to run concurrently, but see the
    advisory lock in ``startup_seed_lock`` for why we would rather not."""

    await _seed_providers(container, settings)
    await _seed_runtime_settings(container, settings)
    await _seed_bootstrap_admin(container, settings)
    await _seed_default_locale(container, settings)
    await _repair_model_preferences(container, settings)
    await _sync_rss_sources(container, settings)
    await _sync_arc_template_pack(container)


async def _seed_providers(
    container: ServiceContainer, settings: AppSettings,
) -> None:
    """Legacy provider-env seed, then build this process's registries from DB.

    The sync is what every other process gets kept in step with afterwards by
    the runtime-config refresher; here it is simply the boot-time baseline.
    """
    await seed_legacy_provider_connections(container, settings)
    await sync_provider_connections(container)


async def _seed_runtime_settings(
    container: ServiceContainer, settings: AppSettings,
) -> None:
    """Site-level runtime settings (Weather/Calendar/GeoIP/NSFW/world-event
    policy): first-boot seed env → ``app_runtime_settings`` when a group's DB row
    is absent, then DB is authoritative. The Weather/Calendar providers were
    already wired from the DB-overlaid settings at container build; here we also
    seed so the Admin「站點設定」page has rows to edit (CORE_ENV_TO_ADMIN_CONFIG
    track 2)."""
    if container.runtime_settings_repository is None:
        return
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


async def _seed_bootstrap_admin(
    container: ServiceContainer, settings: AppSettings,
) -> None:
    """First-run admin bootstrap from ``BOOTSTRAP_ADMIN_*`` env vars.

    No-op once the default user has credentials, so safe to leave in
    long-running deployments. Independent of ``auth.enabled`` — a deployment can
    pre-seed credentials before flipping the auth switch on."""
    if container.auth_service is None:
        return
    try:
        await seed_bootstrap_admin(
            container.auth_service,
            email=settings.auth.bootstrap_admin_email,
            password=settings.auth.bootstrap_admin_password,
        )
    except Exception as exc:  # belt-and-braces; seed is itself fail-soft
        print(f"[lifespan] bootstrap_admin_seed failed: {exc!r}")


async def _seed_default_locale(
    container: ServiceContainer, settings: AppSettings,
) -> None:
    """Deploy-time default UI/content language + timezone for the single-user
    default operator (``USER_PRIMARY_LANGUAGE`` / ``USER_TIMEZONE``, written by
    the self-host installer). Applies only while the default row is unconfigured
    (no password); a real ``/auth/setup`` locks both. Skipped in cloud mode,
    where identity + prefs are federated, not seeded onto a local default row."""
    operator_repo = getattr(container, "operator_profile_repository", None)
    if operator_repo is None or getattr(settings.cloud, "active", False):
        return
    try:
        await seed_default_locale(
            operator_repo,
            language=settings.default_primary_language,
            timezone_id=settings.user_timezone.default_timezone_id,
        )
    except Exception as exc:  # belt-and-braces; seed is itself fail-soft
        print(f"[lifespan] default_locale_seed failed: {exc!r}")


async def _repair_model_preferences(
    container: ServiceContainer, settings: AppSettings,
) -> None:
    """Reset model preferences pointing at providers / model ids no longer
    registered (operator changed env, removed an adapter, unloaded a model in LM
    Studio…). Without this the DB pref keeps shadowing env and the only way back
    is editing the picker by hand or running SQL."""
    pref_validator = ModelPreferenceValidator(
        registry=container.model_registry,
        preferences=container.preferences_repository,
        default_provider_id=settings.default_provider_id,
    )
    try:
        await pref_validator.repair()
    except Exception as exc:  # fail-soft: never block startup on this
        print(f"[lifespan] model preference repair failed: {exc!r}")


async def _sync_rss_sources(
    container: ServiceContainer, settings: AppSettings,
) -> None:
    rss_source_sync = container.rss_source_sync_service
    if rss_source_sync is None:
        return
    try:
        touched = await rss_source_sync.sync()
        if touched:
            print(f"[lifespan] rss_source_sync touched {touched} rows")
    except Exception as exc:  # fail-soft: missing yaml etc.
        print(f"[lifespan] rss_source_sync failed: {exc!r}")
    # First-boot bridge: deprecated KOKORO_WORLD_EVENT_FEED_* env →
    # rss_sources table (CORE_ENV_TO_ADMIN_CONFIG track 3). Only inserts ids
    # not already present, so admin deletions stick.
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


async def _sync_arc_template_pack(container: ServiceContainer) -> None:
    """Arc template pack sync — YAML files under
    ``src/kokoro_link/data/arc_templates/`` upserted as ``user_id=NULL`` rows in
    the ``arc_templates`` table. Same fail-soft policy: a crashed sync leaves
    whatever's in the DB intact rather than blocking startup."""
    arc_template_pack_sync = container.arc_template_pack_sync_service
    if arc_template_pack_sync is None:
        return
    try:
        touched = await arc_template_pack_sync.sync()
        if touched:
            print(
                f"[lifespan] arc_template_pack_sync upserted {touched} rows",
            )
    except Exception as exc:  # fail-soft
        print(f"[lifespan] arc_template_pack_sync failed: {exc!r}")
