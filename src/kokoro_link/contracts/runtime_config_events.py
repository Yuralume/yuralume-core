"""Cross-process runtime-config change signal (PostgreSQL LISTEN/NOTIFY).

Provider settings (``provider_connections``) are the source of truth for every
in-process runtime registry: the LLM model registry, the image/video profile
registries, the TTS backend, the embedder and the ``web_search`` tool. Those
registries are *per process memory*, rebuilt by
:func:`kokoro_link.infrastructure.provider_settings.runtime_sync.sync_provider_connections`.

In a single-process (self-host) deployment the admin write path rebuilds them
inline and that is the whole story. In a multi-process Hosted deployment
(2×api + 2×coordinator + 2×worker + 1×connector against one PostgreSQL) only
the replica that happened to receive the admin HTTP request rebuilt its
registries — every other process kept serving the *old* provider credentials
until it was restarted. Disabling a leaked API key therefore did not take
effect fleet-wide, which is a security problem, not just a staleness one.

This channel is the wake hint that closes that gap: the admin write path fires
a best-effort ``NOTIFY`` after its own sync, and every process runs a
:class:`~kokoro_link.application.services.runtime_config_refresher.RuntimeConfigRefresher`
that re-syncs on receipt. Exactly like the realtime outbox channel, the
notification is *only* a latency optimisation — the durable state lives in the
table, and a fingerprint poll re-converges every process even if every
notification is lost.

Kept in ``contracts`` (not next to either side) so the publisher
(``infrastructure/persistence``) and the subscriber (``application/services``)
share one literal and can never drift onto different channel names.
"""

from __future__ import annotations

RUNTIME_CONFIG_CHANNEL = "yuralume_runtime_config"
"""Well-known PostgreSQL ``LISTEN`` / ``NOTIFY`` channel for config changes.

Deliberately distinct from the realtime-event channel: the two have different
subscriber sets (every role vs the api reader role) and wildly different rates,
so sharing one channel would wake every process on every chat message.
"""

RUNTIME_CONFIG_TOPIC_PROVIDERS = "providers"
"""Payload for a ``provider_connections`` change.

The payload is a coarse *topic*, never a row id or any credential material: the
subscriber always re-reads the table, so the notification carries no state that
could go stale (or leak) in flight. Additional topics can be added here as more
config surfaces become hot-reloadable; an unknown topic is ignored by the
refresher rather than treated as an error.
"""

RUNTIME_CONFIG_TOPIC_SITE_SETTINGS = "site_settings"
"""Payload for an ``app_runtime_settings`` (``site.*``) change.

Same shape and same discipline as the provider topic, for the second config
surface that used to be a boot-time snapshot: the "real world" site settings
(weather coordinates, calendar region, GeoIP endpoint, world-event policy).
``build_container`` overlays them onto :class:`AppSettings` exactly once at
startup, so before this topic existed a hosted fleet of 2×api + coordinator +
worker each held whatever the table said at *its own* boot — changing the site
weather coordinates in Admin took effect on one replica and required restarting
the rest.

Topics are routed, not merely tagged: each subscriber declares the topics it
cares about (:class:`RuntimeConfigRefresher`'s ``topics``), so a site-settings
change does not force every process into a full provider re-sync (which writes
a runtime-status row per enabled provider) and vice versa.
"""
