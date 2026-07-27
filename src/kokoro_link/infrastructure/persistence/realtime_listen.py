"""asyncpg ``LISTEN`` session factory for the realtime dispatcher (Phase 4 §7.1).

The api-side :class:`RealtimeEventDispatcher` turns a best-effort PostgreSQL
``NOTIFY`` into an immediate outbox drain. It stays transport-agnostic (it only
knows the :class:`ListenSession` protocol), so the concrete asyncpg connection
lives here — one dedicated LISTEN connection per api replica, outside the pooled
SQLAlchemy engine (a LISTEN connection is long-lived and must not be recycled by
the request pool).

The dispatcher's ``_listen_loop`` owns the reconnect / backoff discipline; this
module only builds one live connection and hands back a session that blocks on
the next notification. A broken connection surfaces as an exception from
``wait`` / the factory, which the loop catches and rebuilds after its backoff.
"""

from __future__ import annotations

import asyncio
import logging

from kokoro_link.application.services.realtime_event_dispatcher import (
    ListenFactory,
    ListenSession,
)
from kokoro_link.contracts.realtime_events import REALTIME_EVENT_CHANNEL

_LOGGER = logging.getLogger(__name__)


def asyncpg_dsn(database_url: str) -> str:
    """Turn a SQLAlchemy async URL into the plain libpq DSN ``asyncpg`` accepts.

    SQLAlchemy uses a ``postgresql+asyncpg://`` (or ``+psycopg2``) driver suffix
    that ``asyncpg.connect`` does not understand; strip it so the same
    ``DATABASE_URL`` that built the engine also drives the LISTEN connection.
    """
    return database_url.replace("+asyncpg", "").replace("+psycopg2", "")


class AsyncpgListenSession:
    """One live asyncpg ``LISTEN`` connection on a single channel.

    Defaults to :data:`REALTIME_EVENT_CHANNEL`; the runtime-config refresher
    passes its own channel so the two subscriber sets stay disjoint (every
    process listens for config changes, only api replicas tail realtime events).

    ``wait`` blocks until the next notification is delivered to the connection's
    listener callback (asyncpg dispatches notifications on its own reader task,
    so the callback merely hands the payload to an :class:`asyncio.Queue` that
    ``wait`` drains). ``close`` tears the connection down.
    """

    def __init__(
        self, conn: "object", *, channel: str = REALTIME_EVENT_CHANNEL,
    ) -> None:
        self._conn = conn
        self._channel = channel
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def bind(self) -> None:
        await self._conn.add_listener(  # type: ignore[attr-defined]
            self._channel,
            lambda _conn, _pid, _channel, payload: self._queue.put_nowait(
                payload,
            ),
        )

    async def wait(self) -> str | None:
        """Block for the next notification, returning its payload.

        The payload is the coarse *topic* the publisher sent (see
        :mod:`kokoro_link.contracts.runtime_config_events`); subscribers that
        share this channel with another topic route on it. The realtime
        dispatcher ignores the return value — its channel carries one topic.
        """
        return await self._queue.get()

    async def close(self) -> None:
        await self._conn.close()  # type: ignore[attr-defined]


def build_listen_factory(
    database_url: str, *, channel: str = REALTIME_EVENT_CHANNEL,
) -> ListenFactory:
    """Return a :data:`ListenFactory` that opens one bound LISTEN session.

    ``asyncpg`` is imported lazily inside the factory so building the factory
    (done at container construction) never requires the driver or a live
    database — only actually starting the listener opens a connection.

    ``channel`` defaults to the realtime outbox channel; pass an explicit one to
    listen on a different notification stream (the runtime-config refresher).
    """

    dsn = asyncpg_dsn(database_url)

    async def _factory() -> ListenSession:
        import asyncpg  # lazy: no import cost / driver requirement until start

        conn = await asyncpg.connect(dsn)
        session = AsyncpgListenSession(conn, channel=channel)
        await session.bind()
        return session

    return _factory
