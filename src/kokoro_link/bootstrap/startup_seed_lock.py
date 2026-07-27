"""Serialise the startup seed batch across processes (PostgreSQL advisory lock).

The lifespan seed batch (legacy provider seed, ``app_runtime_settings`` seed,
bootstrap admin, default locale, model-preference repair, RSS source sync, arc
template pack sync) is a pile of unlocked read-then-write upserts. In the
single-process self-host shape that is fine — nothing else is running. In a
Hosted deploy, seven processes (2×api + 2×coordinator + 2×worker + 1×connector)
come up within milliseconds of each other and every one of them runs the batch
concurrently against one database, so each read-then-write races the other six:
duplicate inserts, ``IntegrityError`` noise in the logs, and wasted work.

The fix is to serialise, **not** to elect a single seeder. The seeds are
idempotent upserts, so running them seven times in a row is harmless — running
them seven times *at once* is what breaks. A session-level advisory lock turns
the concurrent execution into a queue and the race disappears with no change to
what each process does.

Design notes:

* **Try-and-wait, not block-forever.** We poll ``pg_try_advisory_lock`` to a
  deadline rather than blocking in ``pg_advisory_lock``, so the wait is bounded
  by our own clock and cannot depend on ``lock_timeout`` semantics or leave a
  cancelled statement mid-flight on the connection.
* **Timeout means proceed, not skip.** If the lock cannot be taken in time we
  run the seeds anyway. That is exactly today's behaviour, so the worst case of
  this change is "no worse than before" — never a deployment that silently
  skipped its seeds.
* **Dedicated connection.** Session-level advisory locks live on a connection,
  so the lock is taken on its own AUTOCOMMIT connection, released explicitly and
  closed in a ``finally``. A crashed process drops the connection and PostgreSQL
  releases the lock for us, so the batch can never be wedged by a dead holder.
* **Dialect-gated.** Non-PostgreSQL (SQLite self-host, in-memory test builds,
  no database at all) yields ``False`` immediately and takes no lock: one
  process, no race, byte-identical behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from sqlalchemy import text

_LOGGER = logging.getLogger(__name__)

_POSTGRES_DIALECT = "postgresql"

# Fixed 64-bit advisory-lock key for the startup seed batch. A constant (rather
# than ``hashtext('yuralume:bootstrap-seed')``) keeps the key stable across
# PostgreSQL versions and visible to anyone grepping ``pg_locks``. Mirrors the
# ``_CLAIM_ADMISSION_LOCK_KEY`` convention in ``sa_background_jobs``.
STARTUP_SEED_LOCK_KEY = 7_401_552_930_118_664_517

# Bounded wait. Seven processes × a seed batch of a few seconds fits well
# inside this; past it we stop waiting and seed concurrently (see above).
DEFAULT_SEED_LOCK_TIMEOUT = 90.0
_DEFAULT_RETRY_INTERVAL = 0.5

_TRY_LOCK_SQL = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK_SQL = text("SELECT pg_advisory_unlock(:key)")


def _is_postgres(engine: Any | None) -> bool:
    if engine is None:
        return False
    dialect = getattr(engine, "dialect", None)
    return getattr(dialect, "name", "") == _POSTGRES_DIALECT


async def _open_autocommit(engine: Any) -> Any:
    """Dedicated AUTOCOMMIT connection to hold the session-level lock.

    AUTOCOMMIT matters: the lock is held for the whole seed batch, and a
    connection sitting in an open transaction for that long would be an
    idle-in-transaction holder pinning a snapshot for no reason.
    """
    conn = await engine.connect()
    return await conn.execution_options(isolation_level="AUTOCOMMIT")


@asynccontextmanager
async def startup_seed_lock(
    engine: Any | None,
    *,
    key: int = STARTUP_SEED_LOCK_KEY,
    timeout_seconds: float = DEFAULT_SEED_LOCK_TIMEOUT,
    retry_interval: float = _DEFAULT_RETRY_INTERVAL,
    connect: Callable[[Any], Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> AsyncIterator[bool]:
    """Hold the seed lock for the body. Yields whether it was actually taken.

    The body runs either way — ``False`` only means "we timed out waiting, so
    this run is as concurrent as it was before this helper existed".

    ``connect`` / ``sleep`` / ``monotonic`` are injected so the unit suite can
    drive every branch against a fake connection with no database and no wall
    clock.
    """
    if not _is_postgres(engine):
        yield False
        return

    opener = connect or _open_autocommit
    conn: Any | None = None
    acquired = False
    try:
        conn = await opener(engine)
        acquired = await _acquire(
            conn,
            key=key,
            timeout_seconds=timeout_seconds,
            retry_interval=retry_interval,
            sleep=sleep,
            monotonic=monotonic,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — never block startup on the lock itself
        _LOGGER.warning(
            "startup seed lock unavailable; seeding without it", exc_info=True,
        )
        acquired = False
        if conn is not None:
            await _close_quietly(conn)
            conn = None

    if not acquired and conn is not None:
        # Timed out waiting: drop the connection now rather than holding a
        # pooled one idle for the whole (unlocked) seed batch.
        await _close_quietly(conn)
        conn = None

    try:
        yield acquired
    finally:
        if conn is not None:
            await _release_quietly(conn, key)
            await _close_quietly(conn)


async def _acquire(
    conn: Any,
    *,
    key: int,
    timeout_seconds: float,
    retry_interval: float,
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> bool:
    deadline = monotonic() + max(0.0, timeout_seconds)
    while True:
        result = await conn.execute(_TRY_LOCK_SQL, {"key": key})
        if bool(result.scalar_one()):
            return True
        if monotonic() >= deadline:
            _LOGGER.warning(
                "startup seed lock not acquired within %.1fs; "
                "running seeds unserialised",
                timeout_seconds,
            )
            return False
        await sleep(retry_interval)


async def _release_quietly(conn: Any, key: int) -> None:
    try:
        await conn.execute(_UNLOCK_SQL, {"key": key})
    except Exception:  # noqa: BLE001 — closing the connection also releases it
        _LOGGER.warning("startup seed lock release failed", exc_info=True)


async def _close_quietly(conn: Any) -> None:
    try:
        await conn.close()
    except Exception:  # noqa: BLE001 — shutdown-path hygiene only
        _LOGGER.warning("startup seed lock connection close failed", exc_info=True)
