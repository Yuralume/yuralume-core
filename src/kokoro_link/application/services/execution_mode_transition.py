"""Durable execution-mode cutover and drain-barrier service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from kokoro_link.contracts.background_jobs import (
    BackgroundCoordinatorCursorPort,
    BackgroundJobQueuePort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.execution_mode import (
    MODE_DISTRIBUTED,
    MODE_EMBEDDED,
    MODE_PAUSED,
    RuntimeOwnershipPort,
)

DRAIN_CURSOR_NAME = "execution-mode-drain"


@dataclass(frozen=True, slots=True)
class TransitionResult:
    ok: bool
    code: str = "ok"
    mode: str | None = None
    epoch: int | None = None
    confirmed: bool = False
    claimed: int | None = None


class ExecutionModeTransitionService:
    """Own the shared, durable pause/observe/leave protocol.

    The cursor is deliberately a plain JSON cursor so this protocol can be
    introduced without a schema migration and is shared by HTTP and CLI.
    """

    def __init__(
        self,
        *,
        ownership: RuntimeOwnershipPort,
        queue: BackgroundJobQueuePort,
        cursor: BackgroundCoordinatorCursorPort,
        clock: ClockPort,
        min_observation_interval: float = 1.0,
    ) -> None:
        self._ownership = ownership
        self._queue = queue
        self._cursor = cursor
        self._clock = clock
        self._min_observation_interval = max(0.0, float(min_observation_interval))

    async def enter_paused(self, *, expected_epoch: int) -> TransitionResult:
        current_mode, current_epoch = await self._ownership.get()
        if current_epoch != expected_epoch:
            return TransitionResult(False, "epoch_mismatch", current_mode, current_epoch)
        if current_mode not in (MODE_EMBEDDED, MODE_DISTRIBUTED):
            return TransitionResult(False, "already_paused", current_mode, current_epoch)
        now = ensure_utc(self._clock.now())
        new_epoch = await self._ownership.flip(
            MODE_PAUSED, expected_epoch, now=now,
        )
        if new_epoch is None:
            mode, epoch = await self._ownership.get()
            return TransitionResult(False, "epoch_mismatch", mode, epoch)
        await self._cursor.set(
            DRAIN_CURSOR_NAME,
            {
                "paused_epoch": new_epoch,
                "consecutive_zero": 0,
                "confirmed": False,
                "last_observed_at": None,
            },
        )
        return TransitionResult(True, mode=MODE_PAUSED, epoch=new_epoch)

    async def observe_drain(self, *, paused_epoch: int) -> TransitionResult:
        now = ensure_utc(self._clock.now())
        mode, epoch = await self._ownership.get()
        if mode != MODE_PAUSED or epoch != paused_epoch:
            return TransitionResult(False, "epoch_mismatch", mode, epoch)
        cursor = dict(await self._cursor.get(DRAIN_CURSOR_NAME) or {})
        if cursor.get("paused_epoch") != paused_epoch:
            return TransitionResult(False, "stale_drain_proof", mode, epoch)
        try:
            stats = await self._queue.stats(now=now)
            claimed = int(stats.status_counts.get("claimed", 0))
        except Exception:
            return TransitionResult(False, "drain_unavailable", mode, epoch)

        prior = _parse_timestamp(cursor.get("last_observed_at"))
        consecutive = _as_nonnegative_int(cursor.get("consecutive_zero"))
        confirmed = bool(cursor.get("confirmed"))
        if claimed > 0:
            consecutive = 0
            confirmed = False
        elif prior is not None and now - prior >= timedelta(seconds=self._min_observation_interval):
            consecutive += 1
            confirmed = consecutive >= 2
        elif prior is None:
            consecutive = 1
        # A too-close observation is intentionally not a zero observation.
        cursor.update(
            consecutive_zero=consecutive,
            confirmed=confirmed,
            last_observed_at=now.isoformat(),
        )
        await self._cursor.set(DRAIN_CURSOR_NAME, cursor)
        return TransitionResult(
            True, mode=mode, epoch=epoch, confirmed=confirmed, claimed=claimed,
        )

    async def leave_paused(
        self, target_mode: str, *, expected_epoch: int,
    ) -> TransitionResult:
        if target_mode not in (MODE_EMBEDDED, MODE_DISTRIBUTED):
            return TransitionResult(False, "invalid_target")
        mode, epoch = await self._ownership.get()
        if epoch != expected_epoch:
            return TransitionResult(False, "epoch_mismatch", mode, epoch)
        if mode in (MODE_EMBEDDED, MODE_DISTRIBUTED):
            return TransitionResult(
                False, "paused_barrier_required", mode, epoch,
            )
        if mode != MODE_PAUSED:
            return TransitionResult(False, "epoch_mismatch", mode, epoch)
        cursor = dict(await self._cursor.get(DRAIN_CURSOR_NAME) or {})
        if (
            cursor.get("paused_epoch") != expected_epoch
            or cursor.get("confirmed") is not True
        ):
            return TransitionResult(False, "drain_not_confirmed", mode, epoch)
        now = ensure_utc(self._clock.now())
        new_epoch = await self._ownership.flip(target_mode, expected_epoch, now=now)
        if new_epoch is None:
            latest_mode, latest_epoch = await self._ownership.get()
            return TransitionResult(False, "epoch_mismatch", latest_mode, latest_epoch)
        await self._cursor.set(DRAIN_CURSOR_NAME, {})
        return TransitionResult(True, mode=target_mode, epoch=new_epoch)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def _as_nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
