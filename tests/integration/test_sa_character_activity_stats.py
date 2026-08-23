"""PostgreSQL contract for the hosted character-census read model.

The counts feed an operations card, so the failure that matters is a row
quietly falling out of a bucket: a character whose owner row is gone, a
``NULL`` last-active anchor read as "long ago", a frozen character still
counted as running. Each of those is asserted against a real engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker  # noqa: F401 — type alias for fixtures

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_character_activity_stats import (
    SACharacterActivityStats,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)

_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


async def _operator(session_factory: sessionmaker, operator_id: str, tier: str) -> str:
    await SAOperatorProfileRepository(session_factory).save(OperatorProfile(
        id=operator_id,
        display_name=operator_id,
        cloud_account_id=f"acct-{operator_id}",
        cloud_tenant_id=f"tenant-{operator_id}",
        cloud_tenant_tier=tier,
        auth_provider="cloud",
    ))
    return operator_id


async def _character(
    session_factory: sessionmaker,
    *,
    owner: str,
    name: str,
    last_active_at: datetime | None = None,
) -> Character:
    repository = SACharacterRepository(session_factory)
    character = Character.create(
        name=name,
        summary="",
        user_id=owner,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
            last_active_at=last_active_at,
        ),
    )
    await repository.save(character)
    return character


@pytest.mark.asyncio
async def test_buckets_by_owner_tier_and_excludes_stopped_from_schedulable(
    session_factory: sessionmaker,
) -> None:
    await _operator(session_factory, "op-free", "free")
    await _operator(session_factory, "op-pro", "pro")
    repository = SACharacterRepository(session_factory)

    await _character(session_factory, owner="op-free", name="Running")
    frozen = await _character(session_factory, owner="op-free", name="Frozen")
    locked = await _character(session_factory, owner="op-pro", name="Locked")
    await _character(session_factory, owner="op-pro", name="AlsoRunning")
    await repository.set_frozen(frozen.id, frozen=True, now=_NOW, reason="idle")
    await repository.set_subscription_locked(locked.id, locked=True)

    buckets = {
        bucket.tier: bucket
        for bucket in await SACharacterActivityStats(session_factory).counts_by_tier()
    }

    assert buckets["free"].total == 2
    assert buckets["free"].schedulable == 1
    assert buckets["pro"].total == 2
    assert buckets["pro"].schedulable == 1


@pytest.mark.asyncio
async def test_a_character_whose_owner_row_vanished_is_still_counted(
    session_factory: sessionmaker,
) -> None:
    """The FK says this cannot happen; SQLite says it can, and a census that
    silently shortens itself is worse than one bucketed under the default tier."""
    await _operator(session_factory, "op-gone", "free")
    await _character(session_factory, owner="op-gone", name="Orphan")
    async with session_factory() as session:
        await session.execute(
            text("ALTER TABLE characters DROP CONSTRAINT characters_user_id_fkey"),
        )
        await session.execute(
            text("DELETE FROM operator_profiles WHERE id = 'op-gone'"),
        )
        await session.commit()

    buckets = await SACharacterActivityStats(session_factory).counts_by_tier()

    assert sum(bucket.total for bucket in buckets) == 1
    assert {bucket.tier for bucket in buckets} == {"standard"}


@pytest.mark.asyncio
async def test_engaged_since_counts_only_recent_schedulable_characters_of_that_tier(
    session_factory: sessionmaker,
) -> None:
    await _operator(session_factory, "op-free", "free")
    await _operator(session_factory, "op-pro", "pro")
    repository = SACharacterRepository(session_factory)
    cutoff = _NOW - timedelta(days=7)

    await _character(
        session_factory, owner="op-free", name="Yesterday",
        last_active_at=_NOW - timedelta(days=1),
    )
    await _character(
        session_factory, owner="op-free", name="LongGone",
        last_active_at=_NOW - timedelta(days=30),
    )
    # Never interacted with: NF4 reads a NULL anchor as "never engaged" ⇒
    # dormant, so it must not be counted even though it is younger than the
    # cutoff.
    await _character(session_factory, owner="op-free", name="NeverTouched")
    frozen = await _character(
        session_factory, owner="op-free", name="RecentButFrozen",
        last_active_at=_NOW - timedelta(hours=2),
    )
    await repository.set_frozen(frozen.id, frozen=True, now=_NOW, reason="manual")
    await _character(
        session_factory, owner="op-pro", name="OtherTier",
        last_active_at=_NOW - timedelta(days=1),
    )

    stats = SACharacterActivityStats(session_factory)

    assert await stats.count_engaged_since("free", cutoff) == 1
    assert await stats.count_engaged_since("pro", cutoff) == 1


@pytest.mark.asyncio
async def test_empty_table_reports_no_buckets(session_factory: sessionmaker) -> None:
    assert await SACharacterActivityStats(session_factory).counts_by_tier() == []
