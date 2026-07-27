"""Site-settings hot-reload wiring (G0.1).

Sibling of :mod:`kokoro_link.bootstrap.runtime_config_wiring`, deliberately kept
as its own module so each config surface's "who reloads what, and where" is one
readable, unit-testable decision function rather than another branch inside
``container.py``.

Two pieces:

* :func:`build_site_settings_reloader` — the refresh callback. Re-reads the four
  hot ``site.*`` groups through the process's own session factory, re-applies the
  same overlay the boot path applies, and swaps the result into the holder in one
  atomic rebind. A failed read raises, which the refresher turns into "keep the
  last good snapshot and retry next tick" — reloading is never allowed to
  downgrade a process to env defaults behind the operator's back.
* :func:`build_site_settings_refresher` — the same dialect gate the provider
  refresher uses. PostgreSQL → LISTEN on the shared runtime-config channel,
  filtered to the ``site_settings`` topic, plus the fingerprint-gated fallback
  poll. Anything else → ``None``.

The self-host red line: a single process serves the admin write itself, and
``AppRuntimeSettingsService`` notifies through a no-op on SQLite. Returning
``None`` there keeps that shape byte-identical — except that a self-host process
also holds its settings in the same holder, so the *code path* is shared and only
the cross-process transport is skipped.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsService,
)
from kokoro_link.application.services.runtime_config_refresher import (
    DEFAULT_POLL_INTERVAL,
    RuntimeConfigRefresher,
)
from kokoro_link.bootstrap.app_runtime_settings_seed import (
    apply_site_settings_overrides,
    read_site_settings_groups,
)
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.bootstrap.site_settings_holder import (
    SITE_SETTINGS_HOT_GROUPS,
    SiteSettingsHolder,
    SiteSettingsSnapshot,
)
from kokoro_link.contracts.runtime_config_events import (
    RUNTIME_CONFIG_CHANNEL,
    RUNTIME_CONFIG_TOPIC_SITE_SETTINGS,
)
from kokoro_link.contracts.runtime_settings import RuntimeSettingsRepositoryPort
from kokoro_link.infrastructure.app_runtime_settings.schemas import key_for_group
from kokoro_link.infrastructure.persistence.runtime_config_signal import (
    build_site_settings_fingerprint_reader,
    is_postgres,
)

SITE_SETTINGS_HOT_KEYS: tuple[str, ...] = tuple(
    key_for_group(group) for group in SITE_SETTINGS_HOT_GROUPS
)
"""The ``app_runtime_settings`` keys the fingerprint watches."""


def build_site_settings_reloader(
    *,
    holder: SiteSettingsHolder,
    repository: RuntimeSettingsRepositoryPort | None,
    env_settings: AppSettings,
) -> Callable[[], Awaitable[None]]:
    """Return the callback that re-reads the hot groups into ``holder``.

    ``env_settings`` must be the **env-derived** settings, not the boot-overlaid
    ones: it supplies the per-group fallback when a group's row is absent, and
    seeding it with already-overlaid values would make a deleted row resolve to
    the last DB value instead of the env default.
    """

    service = AppRuntimeSettingsService(repository)

    async def _reload() -> None:
        groups = await read_site_settings_groups(
            service, env_settings, groups=SITE_SETTINGS_HOT_GROUPS,
        )
        overlaid = apply_site_settings_overrides(env_settings, groups)
        holder.replace(
            SiteSettingsSnapshot.from_app_settings(overlaid, source="db"),
        )

    return _reload


def build_site_settings_refresher(
    *,
    engine: Any | None,
    database_url: str,
    reload: Callable[[], Awaitable[None]] | None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> RuntimeConfigRefresher | None:
    """Return this process's site-settings refresher, or ``None`` when unneeded.

    ``reload`` is the container-built callback (it is the same one the admin
    write path runs inline, so the local and remote paths can never diverge);
    ``None`` means this container has no site-settings holder to converge.
    """

    if reload is None or not is_postgres(engine):
        return None

    # Local import keeps the asyncpg dependency off every non-PostgreSQL path
    # (the factory itself imports the driver lazily too, so building it here
    # still opens no connection until the refresher starts).
    from kokoro_link.infrastructure.persistence.realtime_listen import (
        build_listen_factory,
    )

    return RuntimeConfigRefresher(
        refresh=reload,
        fingerprint=build_site_settings_fingerprint_reader(
            engine, keys=SITE_SETTINGS_HOT_KEYS,
        ),
        listen_factory=build_listen_factory(
            database_url, channel=RUNTIME_CONFIG_CHANNEL,
        ),
        poll_interval=poll_interval,
        label="site_settings",
        topics=frozenset({RUNTIME_CONFIG_TOPIC_SITE_SETTINGS}),
    )


__all__ = [
    "SITE_SETTINGS_HOT_KEYS",
    "build_site_settings_refresher",
    "build_site_settings_reloader",
]
