"""PostgreSQL side of the runtime-config refresh signal.

Two tiny engine-backed pieces, kept together because they are the two halves of
one contract (see :mod:`kokoro_link.contracts.runtime_config_events`):

* :func:`notify_runtime_config_changed` — the admin write path's best-effort
  ``NOTIFY`` telling every other process that ``provider_connections`` moved;
* :func:`build_provider_fingerprint_reader` — the subscriber's cheap "did it
  move?" probe, the gate that keeps the fallback poll from turning into a
  fleet-wide write amplifier.

Both are **dialect-gated**: on SQLite (the self-host single-process shape) they
are inert no-ops, because a single process already rebuilds its own registries
inline on the admin write path. Nothing about self-host behaviour changes.

Both are also fail-soft. A lost notification costs latency, never correctness
(the fingerprint poll re-converges); a failed fingerprint read yields ``None``,
which the refresher reads as "unknown, do nothing this tick". Neither may ever
escape into an admin request or a startup path.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import bindparam, text

from kokoro_link.contracts.runtime_config_events import (
    RUNTIME_CONFIG_CHANNEL,
    RUNTIME_CONFIG_TOPIC_PROVIDERS,
)

_LOGGER = logging.getLogger(__name__)

_POSTGRES_DIALECT = "postgresql"

_NOTIFY_SQL = text("SELECT pg_notify(:channel, :payload)")

# Deliberately counts EVERY row, soft-deleted included, and takes the newest
# ``updated_at`` across all of them. That covers all three admin mutations with
# one index-free aggregate: create moves the count, patch moves the max, and
# delete is a soft delete that also bumps ``updated_at``
# (``SAProviderConnectionRepository.delete``). No secret material is read.
_PROVIDER_FINGERPRINT_SQL = text(
    "SELECT count(*) AS row_count, max(updated_at) AS newest "
    "FROM provider_connections",
)

# Deliberately hashes the VALUES rather than aggregating ``updated_at`` the way
# the provider fingerprint does. Two reasons, both specific to this table:
# saving a group re-writes the row even when nothing changed (the admin form
# PUTs the whole blob), and a value hash makes that a genuine no-op instead of a
# fleet-wide reload; and the rows are four tiny JSON blobs, so reading them is
# no more expensive than reading their timestamps.
_SITE_SETTINGS_FINGERPRINT_SQL = text(
    "SELECT key, value FROM app_runtime_settings "
    "WHERE key IN :keys ORDER BY key",
).bindparams(bindparam("keys", expanding=True))


def is_postgres(engine: Any | None) -> bool:
    """Whether ``engine`` speaks PostgreSQL (``None`` / SQLite / fakes → False)."""

    if engine is None:
        return False
    dialect = getattr(engine, "dialect", None)
    return getattr(dialect, "name", "") == _POSTGRES_DIALECT


async def notify_runtime_config_changed(
    engine: Any | None,
    *,
    topic: str = RUNTIME_CONFIG_TOPIC_PROVIDERS,
) -> bool:
    """Fire the cross-process config-change hint. Returns whether it was sent.

    Called by the admin write routes *after* their own local sync, so the
    replica that served the request is already converged before it tells the
    others. Never raises: a dropped hint only delays the other processes to
    their next fingerprint poll, and an admin write must not fail because a
    notification could not be delivered.
    """
    if not is_postgres(engine):
        return False
    try:
        async with engine.begin() as conn:
            await conn.execute(
                _NOTIFY_SQL,
                {"channel": RUNTIME_CONFIG_CHANNEL, "payload": topic},
            )
        return True
    except Exception:  # noqa: BLE001 — a dropped wake hint is non-fatal
        _LOGGER.warning(
            "runtime config NOTIFY failed (topic=%s)", topic, exc_info=True,
        )
        return False


def build_provider_fingerprint_reader(
    engine: Any,
) -> Callable[[], Awaitable[str | None]]:
    """Return a reader yielding an opaque fingerprint of ``provider_connections``.

    The value is compared for equality only — its shape is an implementation
    detail. ``None`` means the read failed and the caller must not infer
    "unchanged" from it.
    """

    async def _read() -> str | None:
        try:
            async with engine.connect() as conn:
                result = await conn.execute(_PROVIDER_FINGERPRINT_SQL)
                row = result.one()
        except Exception:  # noqa: BLE001 — reported as "unknown", never fatal
            _LOGGER.warning(
                "provider settings fingerprint read failed", exc_info=True,
            )
            return None
        count, newest = row[0], row[1]
        stamp = newest.isoformat() if newest is not None else ""
        return f"{int(count or 0)}:{stamp}"

    return _read


def build_site_settings_fingerprint_reader(
    engine: Any,
    *,
    keys: Sequence[str],
) -> Callable[[], Awaitable[str | None]]:
    """Return a reader yielding an opaque fingerprint of the ``site.*`` rows.

    ``keys`` are the ``app_runtime_settings`` keys whose *effective* values this
    process consumes (the hot groups only — see
    :data:`kokoro_link.bootstrap.site_settings_holder.SITE_SETTINGS_HOT_GROUPS`),
    so a change to a group nobody hot-reloads never wakes anyone.

    Compared for equality only. ``None`` means the read failed and the caller
    must not infer "unchanged" from it.
    """

    key_list = list(keys)

    async def _read() -> str | None:
        if not key_list:
            return ""
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    _SITE_SETTINGS_FINGERPRINT_SQL, {"keys": key_list},
                )
                rows = result.all()
        except Exception:  # noqa: BLE001 — reported as "unknown", never fatal
            _LOGGER.warning(
                "site settings fingerprint read failed", exc_info=True,
            )
            return None
        digest = hashlib.sha256()
        for key, value in rows:
            # Length-prefixed so ("ab", "c") and ("a", "bc") cannot collide.
            for part in (str(key), str(value or "")):
                digest.update(f"{len(part)}:".encode())
                digest.update(part.encode("utf-8"))
        return digest.hexdigest()

    return _read
