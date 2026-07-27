"""PostgreSQL integration for the external-chat turn receipt (LH2, §14).

Drives the REAL ``SAExternalChatTurnReceiptRepository`` against Postgres so the
concurrency guarantees the in-memory twin cannot prove hold under true
overlapping transactions on separate asyncpg connections:

* concurrent claim of the same ``(caller, request_id)`` → exactly one
  ``Claimed``, the other ``InProgress`` (UNIQUE insert + ``FOR UPDATE``);
* two claimers racing an EXPIRED lease → exactly one takeover ``Claimed``, the
  other ``InProgress`` (serialised on the locked row);
* a stale (taken-over) owner's ``mark_generated`` is fenced out.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from kokoro_link.contracts.external_chat_turn import Claimed, InProgress
from kokoro_link.infrastructure.persistence.models import (
    ExternalChatTurnReceiptRow,
)
from kokoro_link.infrastructure.persistence.sa_external_chat_turn_repository import (  # noqa: E501
    SAExternalChatTurnReceiptRepository,
)

pytestmark = pytest.mark.asyncio

_PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _claim_kwargs(**over):
    base = dict(
        authenticated_caller="cloud-channel",
        request_id="req-pg",
        canonical_request_hash="hash-a",
        canonical_hash_version=1,
        contract_version=1,
        tenant_id="tenant-1",
        account_id="acct-1",
        character_id="char-1",
        requested_conversation_id=None,
    )
    base.update(over)
    return base


async def _expire_all(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(
            update(ExternalChatTurnReceiptRow).values(lease_until=_PAST),
        )
        await session.commit()


async def test_concurrent_claim_same_request_one_winner(session_factory) -> None:
    repo = SAExternalChatTurnReceiptRepository(session_factory)
    barrier = asyncio.Barrier(2)

    async def _claim():
        await barrier.wait()
        return await repo.claim_or_get(**_claim_kwargs())

    first, second = await asyncio.gather(_claim(), _claim())
    results = [first, second]
    claimed = [r for r in results if isinstance(r, Claimed)]
    in_progress = [r for r in results if isinstance(r, InProgress)]
    # Exactly one caller owns the fresh turn; the other sees it in flight.
    assert len(claimed) == 1
    assert len(in_progress) == 1
    # Both refer to the same single receipt row.
    assert claimed[0].receipt.turn_id == in_progress[0].receipt.turn_id


async def test_concurrent_expired_takeover_one_winner(session_factory) -> None:
    repo = SAExternalChatTurnReceiptRepository(session_factory)
    seed = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(seed, Claimed)
    await _expire_all(session_factory)

    barrier = asyncio.Barrier(2)

    async def _claim():
        await barrier.wait()
        return await repo.claim_or_get(**_claim_kwargs())

    first, second = await asyncio.gather(_claim(), _claim())
    results = [first, second]
    claimed = [r for r in results if isinstance(r, Claimed)]
    in_progress = [r for r in results if isinstance(r, InProgress)]
    # The expired lease is taken over by exactly ONE claimer.
    assert len(claimed) == 1
    assert len(in_progress) == 1
    taken = claimed[0].receipt
    assert taken.turn_id == seed.receipt.turn_id
    assert taken.lease_version == 2  # bumped exactly once
    assert taken.attempt_count == 1
    assert taken.owner_id != seed.receipt.owner_id


async def test_stale_owner_mark_generated_is_rejected(session_factory) -> None:
    repo = SAExternalChatTurnReceiptRepository(session_factory)
    first = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(first, Claimed)
    original = first.receipt

    # Force a takeover so lease_version advances past the original owner.
    await _expire_all(session_factory)
    taken = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(taken, Claimed)
    new = taken.receipt
    assert new.lease_version == 2

    # The stale original owner cannot checkpoint GENERATED…
    assert not await repo.mark_generated(
        turn_id=original.turn_id, owner_id=original.owner_id,
        lease_version=1, generated_snapshot_json="STALE",
    )
    # …but the current owner can.
    assert await repo.mark_generated(
        turn_id=new.turn_id, owner_id=new.owner_id,
        lease_version=new.lease_version, generated_snapshot_json="FRESH",
    )
    final = await repo.get(original.turn_id)
    assert final.generated_snapshot_json == "FRESH"
