"""Cross-replica safety for post-turn memory consolidation.

``AutoConsolidationTrigger`` gated itself with three *per-process* structures —
a ``_last_run_at`` dict (6h cooldown), a ``_running`` set (single-flight) and a
per-character ``asyncio.Lock``. ``maybe_trigger`` is fired fire-and-forget from
the chat turn, which the hosted topology serves from several api replicas, so
none of them was mutual exclusion: two replicas could run ``consolidate()`` —
an LLM-heavy pass that rewrites memory rows — for the same character at once,
and the effective cooldown shrank in proportion to the replica count.

The gate is now ``claim_consolidation_slot`` on the shared character
repository: one conditional UPDATE that answers cooldown and single-flight
together. These tests wire TWO trigger instances (as two replicas would be)
over ONE character repository.

The SQLite half proves the real conditional UPDATE, so a rewrite that loses the
``last_consolidated_at IS NULL OR ... < cutoff`` fence fails here rather than in
production.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.application.services.auto_consolidation_trigger import (
    AutoConsolidationTrigger,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import CharacterRow
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)

pytestmark = pytest.mark.asyncio

COOLDOWN = timedelta(hours=6)


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class _YieldingCountingRepo:
    """Memory pool count — a real suspension point so the triggers interleave."""

    def __init__(self, count: int) -> None:
        self._count = count

    async def count_for_character(self, character_id: str) -> int:
        await asyncio.sleep(0)
        return self._count


class _SlowConsolidationService:
    """Stands in for the decay + merge LLM pass."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def consolidate(self, character_id: str, **_: object):  # noqa: ANN201
        self.calls.append(character_id)
        await asyncio.sleep(0.05)
        return _Report(
            character_id=character_id,
            dry_run=False,
            decayed=0,
            clusters_found=0,
            clusters_merged=0,
            memories_replaced=0,
            memories_after=0,
        )


class _CrashingConsolidationService(_SlowConsolidationService):
    async def consolidate(self, character_id: str, **_: object):  # noqa: ANN201
        self.calls.append(character_id)
        raise RuntimeError("boom")


@dataclass
class _Report:
    character_id: str
    dry_run: bool
    decayed: int
    clusters_found: int
    clusters_merged: int
    memories_replaced: int
    memories_after: int


def _trigger(
    *,
    characters,  # noqa: ANN001
    service,  # noqa: ANN001
    count: int = 100,
) -> AutoConsolidationTrigger:
    return AutoConsolidationTrigger(
        memory_repository=_YieldingCountingRepo(count),  # type: ignore[arg-type]
        consolidation_service=service,  # type: ignore[arg-type]
        character_repository=characters,
        threshold=10,
        cooldown=COOLDOWN,
    )


# --------------------------------------------------------------------------- #
# In-memory twin — two "replicas" over one repository.
# --------------------------------------------------------------------------- #
async def test_two_replicas_consolidate_once() -> None:
    clock = _Clock(now=datetime(2026, 4, 19, 12, tzinfo=timezone.utc))
    characters = InMemoryCharacterRepository(clock=clock)
    service = _SlowConsolidationService()
    replica_a = _trigger(characters=characters, service=service)
    replica_b = _trigger(characters=characters, service=service)

    results = await asyncio.gather(
        replica_a.maybe_trigger("c1"),
        replica_b.maybe_trigger("c1"),
    )

    assert results.count(True) == 1
    assert service.calls == ["c1"], (
        "both replicas ran the consolidation LLM pass on the same character"
    )


async def test_second_replica_is_refused_for_the_whole_cooldown() -> None:
    clock = _Clock(now=datetime(2026, 4, 19, 12, tzinfo=timezone.utc))
    characters = InMemoryCharacterRepository(clock=clock)
    service = _SlowConsolidationService()
    replica_a = _trigger(characters=characters, service=service)
    replica_b = _trigger(characters=characters, service=service)

    assert await replica_a.maybe_trigger("c1") is True
    clock.advance(COOLDOWN - timedelta(minutes=1))

    # A *different* process — its own empty ``_last_run_at`` in the old design.
    assert await replica_b.maybe_trigger("c1") is False
    assert service.calls == ["c1"]


async def test_either_replica_may_run_again_after_the_cooldown() -> None:
    clock = _Clock(now=datetime(2026, 4, 19, 12, tzinfo=timezone.utc))
    characters = InMemoryCharacterRepository(clock=clock)
    service = _SlowConsolidationService()
    replica_a = _trigger(characters=characters, service=service)
    replica_b = _trigger(characters=characters, service=service)

    assert await replica_a.maybe_trigger("c1") is True
    clock.advance(COOLDOWN + timedelta(minutes=1))

    assert await replica_b.maybe_trigger("c1") is True
    assert service.calls == ["c1", "c1"]


async def test_characters_are_claimed_independently() -> None:
    clock = _Clock(now=datetime(2026, 4, 19, 12, tzinfo=timezone.utc))
    characters = InMemoryCharacterRepository(clock=clock)
    service = _SlowConsolidationService()
    replica_a = _trigger(characters=characters, service=service)
    replica_b = _trigger(characters=characters, service=service)

    results = await asyncio.gather(
        replica_a.maybe_trigger("a"),
        replica_b.maybe_trigger("b"),
    )

    assert results == [True, True]
    assert sorted(service.calls) == ["a", "b"]


async def test_a_crashing_run_still_consumes_the_cooldown() -> None:
    """Matches the historical "stamp before running" semantics: no hot loop."""
    clock = _Clock(now=datetime(2026, 4, 19, 12, tzinfo=timezone.utc))
    characters = InMemoryCharacterRepository(clock=clock)
    service = _CrashingConsolidationService()
    replica_a = _trigger(characters=characters, service=service)
    replica_b = _trigger(characters=characters, service=service)

    assert await replica_a.maybe_trigger("c1") is False
    assert await replica_b.maybe_trigger("c1") is False
    assert service.calls == ["c1"]


async def test_below_threshold_does_not_consume_the_claim() -> None:
    clock = _Clock(now=datetime(2026, 4, 19, 12, tzinfo=timezone.utc))
    characters = InMemoryCharacterRepository(clock=clock)
    service = _SlowConsolidationService()
    small = _trigger(characters=characters, service=service, count=1)
    big = _trigger(characters=characters, service=service, count=100)

    assert await small.maybe_trigger("c1") is False
    assert await big.maybe_trigger("c1") is True


async def test_unclaimable_repository_fails_closed() -> None:
    class _BrokenRepo(InMemoryCharacterRepository):
        async def claim_consolidation_slot(self, character_id, *, cooldown):  # noqa: ANN001, ANN201
            raise RuntimeError("db down")

    service = _SlowConsolidationService()
    trigger = _trigger(characters=_BrokenRepo(), service=service)

    assert await trigger.maybe_trigger("c1") is False
    assert service.calls == []


# --------------------------------------------------------------------------- #
# SQL adapter — the real conditional UPDATE.
# --------------------------------------------------------------------------- #
def _character() -> Character:
    return Character.create(
        name="Yui",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


class _SACharacterFixture:
    """The repository plus a back-door to age the stored claim.

    The adapter's time authority is the DB clock and deliberately not
    injectable (a replica must not be able to shorten the shared cooldown with
    a skewed wall clock), so "the cooldown has lapsed" is simulated by moving
    the stored stamp into the past instead of moving the clock forward.
    """

    def __init__(self, repo: SACharacterRepository, engine) -> None:  # noqa: ANN001
        self.repo = repo
        self._engine = engine

    async def age_claim(self, character_id: str, *, by: timedelta) -> None:
        async with self._engine.begin() as conn:
            row = (
                await conn.exec_driver_sql(
                    "SELECT last_consolidated_at FROM characters WHERE id = ?",
                    (character_id,),
                )
            ).scalar_one()
            stamped = datetime.fromisoformat(row)
            await conn.exec_driver_sql(
                "UPDATE characters SET last_consolidated_at = ? WHERE id = ?",
                ((stamped - by).isoformat(sep=" "), character_id),
            )


@pytest_asyncio.fixture()
async def sa_fixture():  # noqa: ANN201
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: CharacterRow.__table__.create(sync))
    try:
        yield _SACharacterFixture(
            SACharacterRepository(build_session_factory(engine)), engine,
        )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def sa_characters(sa_fixture) -> SACharacterRepository:  # noqa: ANN001
    return sa_fixture.repo


async def test_sql_claim_is_granted_once_then_refused(sa_characters) -> None:  # noqa: ANN001
    character = _character()
    await sa_characters.save(character)

    first = await sa_characters.claim_consolidation_slot(
        character.id, cooldown=COOLDOWN,
    )
    second = await sa_characters.claim_consolidation_slot(
        character.id, cooldown=COOLDOWN,
    )

    assert first is True
    assert second is False


async def test_sql_claim_is_granted_again_once_the_cooldown_lapsed(
    sa_fixture,
) -> None:  # noqa: ANN001
    character = _character()
    await sa_fixture.repo.save(character)
    assert await sa_fixture.repo.claim_consolidation_slot(
        character.id, cooldown=COOLDOWN,
    ) is True

    await sa_fixture.age_claim(character.id, by=COOLDOWN - timedelta(hours=1))
    assert await sa_fixture.repo.claim_consolidation_slot(
        character.id, cooldown=COOLDOWN,
    ) is False, "a claim inside the window must stay refused"

    await sa_fixture.age_claim(character.id, by=timedelta(hours=2))
    assert await sa_fixture.repo.claim_consolidation_slot(
        character.id, cooldown=COOLDOWN,
    ) is True


async def test_sql_claim_ignores_unknown_characters(sa_characters) -> None:  # noqa: ANN001
    assert await sa_characters.claim_consolidation_slot(
        "nope", cooldown=COOLDOWN,
    ) is False


async def test_aggregate_save_never_resurrects_a_consumed_claim(
    sa_characters,
) -> None:  # noqa: ANN001
    character = _character()
    await sa_characters.save(character)
    assert await sa_characters.claim_consolidation_slot(
        character.id, cooldown=COOLDOWN,
    ) is True

    await sa_characters.save(character)

    assert await sa_characters.claim_consolidation_slot(
        character.id, cooldown=COOLDOWN,
    ) is False
