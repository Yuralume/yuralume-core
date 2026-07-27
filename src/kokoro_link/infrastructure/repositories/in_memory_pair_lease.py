"""In-process pair lease — parity backend for the SA adapter (Phase 5 §6.1).

Byte-for-byte the same observable contract as
:class:`kokoro_link.infrastructure.persistence.sa_pair_lease.SAPairLease`, so a
single self-host process behaves identically to the distributed path and the
parity tests can pin both. A single :class:`asyncio.Lock` makes the two-row
acquire atomic in-process, standing in for the SA single-transaction /
row-lock-ordering guarantee.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.pair_lease import (
    PairLeaseHandle,
    canonical_pair,
    pair_lease_name,
)


_EPOCH_ZERO = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(slots=True)
class _Record:
    token: str
    epoch: int
    lease_until: datetime


class InMemoryPairLease:
    """In-process atomic two-character TTL lease."""

    def __init__(self) -> None:
        self._rows: dict[str, _Record] = {}
        self._mutex = asyncio.Lock()

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
        names = (pair_lease_name(low), pair_lease_name(high))
        async with self._mutex:
            # First pass: both rows must be free/expired (the lock makes this
            # check-then-set atomic, mirroring the SA single transaction).
            for name in names:
                rec = self._rows.get(name)
                if rec is not None and rec.lease_until > now:
                    return None  # live lease held by another holder → no residual
            # Second pass: commit the takeover on both rows together.
            for name in names:
                rec = self._rows.get(name)
                if rec is None:
                    self._rows[name] = _Record(token, 1, until)
                else:
                    rec.epoch += 1
                    rec.token = token
                    rec.lease_until = until
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
        async with self._mutex:
            for name in handle.names:
                rec = self._rows.get(name)
                if (
                    rec is None
                    or rec.token != handle.token
                    or rec.lease_until < now
                ):
                    return False  # lost: taken over or expired → renew nothing
            for name in handle.names:
                self._rows[name].lease_until = until
            return True

    async def release(self, handle: PairLeaseHandle) -> None:
        async with self._mutex:
            for name in handle.names:
                rec = self._rows.get(name)
                if rec is None or rec.token != handle.token:
                    continue  # taken over / never ours → leave untouched
                # Keep the epoch (monotonic); vacate ownership.
                rec.token = ""
                rec.lease_until = _EPOCH_ZERO
