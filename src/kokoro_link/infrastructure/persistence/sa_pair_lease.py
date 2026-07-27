"""SQLAlchemy adapter for the encounter pair lease (Phase 5 §6.1).

Reuses the generic ``background_runtime_leases`` table under the
``encounter:<char_id>`` name namespace — the same TTL + monotonic-epoch row
shape the coordinator and studio leases use — but with pair-atomic semantics:
``acquire`` takes BOTH per-character rows in ONE transaction, always in
ascending row order, and either wins both or rolls back leaving no residual.

Why a dedicated adapter rather than two calls to
:class:`SABackgroundCoordinatorLease`: two separate transactions could leave a
half-held pair (row A committed, row B denied) and could deadlock a crossing
pair. The single-transaction, single-lock-order acquire here is what makes the
pair atomic and deadlock-free (see ``contracts/pair_lease.py`` for the proof).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.pair_lease import (
    PairLeaseHandle,
    canonical_pair,
    pair_lease_name,
)
from kokoro_link.infrastructure.persistence.models import (
    BackgroundRuntimeLeaseRow,
)


_EPOCH_ZERO = datetime(1970, 1, 1, tzinfo=timezone.utc)


class SAPairLease:
    """Atomic two-character TTL lease over ``background_runtime_leases``."""

    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def acquire(
        self,
        char_a: str,
        char_b: str,
        owner: str,
        *,
        ttl_seconds: int,
        now: datetime,
    ) -> PairLeaseHandle | None:
        low, high = canonical_pair(char_a, char_b)
        now = ensure_utc(now)
        until = now + timedelta(seconds=ttl_seconds)
        token = uuid4().hex
        # Names are ``encounter:<id>``; ``low < high`` ⇒ name(low) < name(high),
        # so locking in (low, high) order is the single global lock order that
        # makes crossing pairs deadlock-free.
        names = (pair_lease_name(low), pair_lease_name(high))
        async with self._session_factory() as session:
            try:
                for name in names:
                    row = await session.get(
                        BackgroundRuntimeLeaseRow, name, with_for_update=True,
                    )
                    if row is None:
                        # No row yet: insert our claim. A concurrent inserter of
                        # the same PK trips IntegrityError (here on the next
                        # statement's autoflush, or at commit) → this acquire
                        # loses the pair as a whole, never a half-hold.
                        session.add(BackgroundRuntimeLeaseRow(
                            name=name,
                            owner_id=token,
                            epoch=1,
                            lease_until=until,
                            updated_at=now,
                        ))
                        continue
                    if ensure_utc(row.lease_until) > now:
                        # Live lease held by another holder (token is always
                        # fresh, so this is never us) → the whole pair is
                        # unavailable.
                        await session.rollback()
                        return None
                    # Free / expired → take it over; ownership change bumps the
                    # monotonic epoch so a stale predecessor is fenced out.
                    row.epoch += 1
                    row.owner_id = token
                    row.lease_until = until
                    row.updated_at = now
                await session.commit()
            except IntegrityError:
                # A concurrent acquire inserted one of our new rows first
                # (FOR UPDATE cannot gap-lock a not-yet-existent PK). We lost the
                # contested character → the whole pair fails, no residual.
                await session.rollback()
                return None
        return PairLeaseHandle(
            char_a=low,
            char_b=high,
            owner=owner,
            token=token,
            ttl_seconds=ttl_seconds,
            lease_until=until,
        )

    async def heartbeat(
        self, handle: PairLeaseHandle, *, now: datetime,
    ) -> bool:
        now = ensure_utc(now)
        until = now + timedelta(seconds=handle.ttl_seconds)
        async with self._session_factory() as session:
            rows = []
            for name in handle.names:
                row = await session.get(
                    BackgroundRuntimeLeaseRow, name, with_for_update=True,
                )
                if (
                    row is None
                    or row.owner_id != handle.token
                    or ensure_utc(row.lease_until) < now
                ):
                    # Lost: taken over, or expired (an expired lease is not
                    # renewable by its former holder). Renew NOTHING so we never
                    # leave a half-renewed pair.
                    await session.rollback()
                    return False
                rows.append(row)
            for row in rows:
                row.lease_until = until
                row.updated_at = now
            await session.commit()
            return True

    async def release(self, handle: PairLeaseHandle) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            for name in handle.names:
                row = await session.get(
                    BackgroundRuntimeLeaseRow, name, with_for_update=True,
                )
                if row is None or row.owner_id != handle.token:
                    # Already taken over / never held under this token: leave the
                    # new owner's row untouched.
                    continue
                # Keep the epoch (monotonic); vacate ownership so the next
                # acquire counts as a change and bumps it.
                row.owner_id = ""
                row.lease_until = _EPOCH_ZERO
                row.updated_at = now
            await session.commit()
