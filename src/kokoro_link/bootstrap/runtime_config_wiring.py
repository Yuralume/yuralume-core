"""Runtime-config refresher wiring (one pure decision function).

Kept out of ``container.py`` for the same reason ``realtime_wiring`` is: the
"which deployment shape gets which component" decision is worth unit-testing on
its own, without a database or a full container build.

The rule is deliberately *not* role-gated. Every process that holds provider
registries must converge on a provider change — including ``coordinator`` /
``worker`` / ``connector``, which serve no admin route and therefore had no
other way to ever learn about one. The only gate is the dialect:

* PostgreSQL → build the refresher (LISTEN + fingerprint-gated fallback poll);
* anything else (SQLite self-host, in-memory / no database) → ``None``.

The self-host red line: a single process already rebuilds its own registries
inline on the admin write path, so there is nothing to converge with and no
LISTEN transport to do it over. Returning ``None`` there keeps that shape
byte-identical — no extra task, no extra connection, no extra queries.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from kokoro_link.application.services.runtime_config_refresher import (
    DEFAULT_POLL_INTERVAL,
    RuntimeConfigRefresher,
)
from kokoro_link.contracts.runtime_config_events import (
    RUNTIME_CONFIG_CHANNEL,
    RUNTIME_CONFIG_TOPIC_PROVIDERS,
)
from kokoro_link.infrastructure.persistence.runtime_config_signal import (
    build_provider_fingerprint_reader,
    is_postgres,
)


def build_runtime_config_refresher(
    *,
    engine: Any | None,
    database_url: str,
    refresh: Callable[[], Awaitable[None]],
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> RuntimeConfigRefresher | None:
    """Return this process's refresher, or ``None`` when it needs none."""

    if not is_postgres(engine):
        return None

    # Local import keeps the asyncpg dependency off every non-PostgreSQL path
    # (the factory itself imports the driver lazily too, so building it here
    # still opens no connection until the refresher starts).
    from kokoro_link.infrastructure.persistence.realtime_listen import (
        build_listen_factory,
    )

    return RuntimeConfigRefresher(
        refresh=refresh,
        fingerprint=build_provider_fingerprint_reader(engine),
        listen_factory=build_listen_factory(
            database_url, channel=RUNTIME_CONFIG_CHANNEL,
        ),
        poll_interval=poll_interval,
        # The channel is shared with the site-settings topic, and the two
        # refreshes are not the same price: this one writes a runtime-status row
        # per enabled provider, so waking it on an unrelated topic would
        # multiply a single Admin save into (processes × providers) writes —
        # exactly the amplification the fingerprint gate exists to prevent.
        topics=frozenset({RUNTIME_CONFIG_TOPIC_PROVIDERS}),
    )
