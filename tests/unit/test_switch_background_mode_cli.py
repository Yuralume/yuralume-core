"""Unit smoke for the §15 mode-switch CLI (argparse, dry-run, guard).

No database: exercises the pure argument surface, the prod-safety guard, and
the two direction handlers in dry-run mode against the in-memory ownership twin
+ a fake queue — proving dry-run writes nothing.
"""

from __future__ import annotations

import argparse

import pytest

from kokoro_link.cli import switch_background_mode as cli
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryRuntimeOwnership,
)


class _FakeStats:
    def __init__(self, claimed: int) -> None:
        self.status_counts = {"claimed": claimed}


class _FakeQueue:
    def __init__(self, claimed: int = 0) -> None:
        self._claimed = claimed

    async def stats(self, *, now):
        return _FakeStats(self._claimed)


def test_parser_defaults_and_choices() -> None:
    args = cli.build_parser().parse_args(["--to", "distributed"])
    assert args.to == "distributed"
    assert args.apply is False
    assert args.drain_timeout == 300.0
    assert args.poll_interval == 5.0
    assert args.i_know is False


def test_parser_rejects_bad_mode() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--to", "sideways"])


def test_guard_refuses_without_url_or_optin(capsys) -> None:
    args = argparse.Namespace(database_url="", i_know=False)
    assert cli._resolve_database_url(args) is None
    err = capsys.readouterr().err
    assert "refusing" in err


def test_guard_explicit_url_bypasses_optin() -> None:
    args = argparse.Namespace(database_url="postgresql+asyncpg://x/y", i_know=True)
    assert cli._resolve_database_url(args) == "postgresql+asyncpg://x/y"


def test_guard_optin_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env/db")
    args = argparse.Namespace(database_url="", i_know=True)
    assert cli._resolve_database_url(args) == "postgresql+asyncpg://env/db"


@pytest.mark.asyncio
async def test_dry_run_to_distributed_writes_nothing() -> None:
    ownership = InMemoryRuntimeOwnership()
    queue = _FakeQueue(claimed=0)
    rc = await cli._to_distributed(
        ownership, queue, current_epoch=0, apply=False,
    )
    assert rc == 0
    # Dry run must not flip.
    assert await ownership.get() == ("embedded", 0)


@pytest.mark.asyncio
async def test_dry_run_to_embedded_writes_nothing() -> None:
    ownership = InMemoryRuntimeOwnership()
    # Pretend we start in distributed at epoch 1.
    await ownership.flip("distributed", 0, now=_now())
    queue = _FakeQueue(claimed=2)
    rc = await cli._to_embedded(
        ownership, queue, current_epoch=1, apply=False, drain_timeout=1.0,
    )
    assert rc == 0
    # Dry run must not flip back.
    assert await ownership.get() == ("distributed", 1)


@pytest.mark.asyncio
async def test_apply_to_distributed_flips_and_returns_zero() -> None:
    ownership = InMemoryRuntimeOwnership()
    queue = _FakeQueue(claimed=0)
    rc = await cli._to_distributed(
        ownership, queue, current_epoch=0, apply=True,
    )
    assert rc == 0
    assert await ownership.get() == ("distributed", 2)


@pytest.mark.asyncio
async def test_apply_to_embedded_flips_first_then_drains() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip("distributed", 0, now=_now())
    queue = _FakeQueue(claimed=0)  # already drained
    rc = await cli._to_embedded(
        ownership, queue, current_epoch=1, apply=True, drain_timeout=5.0,
        poll_interval=0.0,
    )
    assert rc == 0
    assert await ownership.get() == ("embedded", 3)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
