"""Startup seed batch serialisation (PostgreSQL advisory lock).

The bug: the lifespan seed batch runs before any role gate, so all seven Hosted
processes execute the same unlocked read-then-write upserts within milliseconds
of each other against one database.

The contract pinned here: PostgreSQL takes a session-level advisory lock on a
dedicated connection and releases it; every other dialect (SQLite self-host,
no database) takes no lock at all; and in every failure mode the body still
runs, because "seeds skipped" would be worse than the race we are fixing.

Driven against a fake connection — the lock protocol is two SQL statements, and
a real PostgreSQL round-trip would not pin any branch this does not.
"""

from __future__ import annotations

import pytest

from kokoro_link.bootstrap.startup_seed_lock import (
    STARTUP_SEED_LOCK_KEY,
    startup_seed_lock,
)

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _FakeConn:
    """Records every statement; ``free_after`` simulates a contended lock."""

    def __init__(self, *, free_after: int = 0, fail_on_execute: bool = False) -> None:
        self.free_after = free_after
        self.fail_on_execute = fail_on_execute
        self.statements: list[tuple[str, dict]] = []
        self.closed = False
        self._attempts = 0

    async def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append((sql, dict(parameters or {})))
        if self.fail_on_execute:
            raise RuntimeError("connection lost")
        if "pg_try_advisory_lock" in sql:
            self._attempts += 1
            return _Result(self._attempts > self.free_after)
        return _Result(True)

    async def close(self) -> None:
        self.closed = True

    def calls(self, needle: str) -> int:
        return sum(1 for sql, _ in self.statements if needle in sql)


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEngine:
    def __init__(self, name: str) -> None:
        self.dialect = _FakeDialect(name)


def _connector(conn):
    async def _connect(_engine):
        return conn
    return _connect


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_postgres_takes_and_releases_the_lock() -> None:
    conn = _FakeConn()
    ran = False
    async with startup_seed_lock(
        _FakeEngine("postgresql"), connect=_connector(conn),
    ) as acquired:
        assert acquired is True
        # Still held while the body runs.
        assert conn.calls("pg_advisory_unlock") == 0
        assert conn.closed is False
        ran = True
    assert ran is True
    assert conn.calls("pg_try_advisory_lock") == 1
    assert conn.calls("pg_advisory_unlock") == 1
    assert conn.closed is True
    assert conn.statements[0][1] == {"key": STARTUP_SEED_LOCK_KEY}


async def test_contended_lock_waits_then_acquires() -> None:
    """Serialise, don't skip: a busy lock is retried until it frees."""
    conn = _FakeConn(free_after=2)
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    async with startup_seed_lock(
        _FakeEngine("postgresql"),
        connect=_connector(conn),
        retry_interval=0.25,
        sleep=_sleep,
    ) as acquired:
        assert acquired is True
    assert conn.calls("pg_try_advisory_lock") == 3
    assert slept == [0.25, 0.25]


async def test_timeout_still_runs_the_body_unlocked() -> None:
    """Worst case is today's behaviour — never a deployment that skipped seeds."""
    conn = _FakeConn(free_after=10_000)
    clock = iter([0.0, 0.0, 5.0, 99.0])

    async with startup_seed_lock(
        _FakeEngine("postgresql"),
        connect=_connector(conn),
        timeout_seconds=1.0,
        sleep=_no_sleep,
        monotonic=lambda: next(clock),
    ) as acquired:
        assert acquired is False
    # Connection dropped rather than held idle through the unlocked batch.
    assert conn.closed is True
    assert conn.calls("pg_advisory_unlock") == 0


async def test_sqlite_takes_no_lock() -> None:
    """Self-host red line: single process, no contention, no extra SQL."""
    conn = _FakeConn()
    async with startup_seed_lock(
        _FakeEngine("sqlite"), connect=_connector(conn),
    ) as acquired:
        assert acquired is False
    assert conn.statements == []
    assert conn.closed is False


async def test_no_engine_takes_no_lock() -> None:
    async with startup_seed_lock(None) as acquired:
        assert acquired is False


async def test_connection_failure_is_fail_soft() -> None:
    async def _boom(_engine):
        raise RuntimeError("pool exhausted")

    ran = False
    async with startup_seed_lock(
        _FakeEngine("postgresql"), connect=_boom,
    ) as acquired:
        assert acquired is False
        ran = True
    assert ran is True


async def test_lock_statement_failure_is_fail_soft_and_closes() -> None:
    conn = _FakeConn(fail_on_execute=True)
    async with startup_seed_lock(
        _FakeEngine("postgresql"), connect=_connector(conn),
    ) as acquired:
        assert acquired is False
    assert conn.closed is True


async def test_body_exception_still_releases_the_lock() -> None:
    conn = _FakeConn()
    with pytest.raises(ValueError):
        async with startup_seed_lock(
            _FakeEngine("postgresql"), connect=_connector(conn),
        ):
            raise ValueError("seed blew up")
    assert conn.calls("pg_advisory_unlock") == 1
    assert conn.closed is True
