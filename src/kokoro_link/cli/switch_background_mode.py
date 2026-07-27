"""Switch the background execution mode (HOSTED_CORE_SCALING §15 rollback).

Automates both directions of the §15 runbook as a CAS flip on the
``background_execution_mode`` row, so the embedded ↔ distributed transition is
driven by the DB ownership epoch — never by the human assumption that two
process fleets won't run at once. Direct embedded↔distributed flips are
forbidden; every cutover passes through paused.

    # Dry run (default) — read + print the plan, write nothing.
    python -m kokoro_link.cli.switch_background_mode \
        --to distributed --database-url postgresql+asyncpg://...

    # Actually flip.
    python -m kokoro_link.cli.switch_background_mode \
        --to embedded --database-url postgresql+asyncpg://... --apply

Directions:

* ``embedded → distributed`` (§15 "Embedded → Distributed"): enter paused,
  verify no claimed jobs remain, then enter distributed so the coordinator/worker
  fleet may begin; print the canary next steps.
* ``distributed → embedded`` (§15 steps 1–5): flip the mode FIRST — that stops
  the coordinator enqueue + pauses workers at their next ownership check (P3-C
  behavior) — then POLL the queue until ``claimed == 0`` (or ``--drain-timeout``
  elapses) before printing the verification summary + "start one embedded
  scheduler" next step.

Prod-safety guard (mirrors the soak scripts): the tool refuses to touch the
ambient-environment database unless you either pass ``--database-url`` OR pass
``--i-know``. Never prints the database URL or any secret.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from kokoro_link.contracts.execution_mode import (
    MODE_DISTRIBUTED,
    MODE_EMBEDDED,
    MODE_PAUSED,
)
from kokoro_link.application.services.execution_mode_transition import (
    ExecutionModeTransitionService,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorCursor,
)
from kokoro_link.infrastructure.time.system_clock import SystemClock

_LOGGER = logging.getLogger(__name__)

_DRAIN_POLL_SECONDS = 5.0


def _resolve_database_url(args: argparse.Namespace) -> str | None:
    if args.database_url:
        return args.database_url
    if not args.i_know:
        print(
            "refusing to flip the mode against the ambient-environment "
            "DATABASE_URL without an explicit opt-in; pass --database-url or "
            "--i-know to target it.",
            file=sys.stderr,
        )
        return None
    env_url = os.getenv("DATABASE_URL", os.getenv("KOKORO_DATABASE_URL", ""))
    if not env_url:
        print("no DATABASE_URL / KOKORO_DATABASE_URL set.", file=sys.stderr)
        return None
    return env_url


async def _claimed_count(queue, *, now: datetime) -> int | None:
    """Current ``claimed`` job count, or ``None`` if the queue tables are
    missing / unreadable (surfaces the 'run migrations first' hint)."""
    try:
        stats = await queue.stats(now=now)
    except Exception as exc:  # tables missing or DB error
        _LOGGER.error("could not read queue stats: %s", type(exc).__name__)
        return None
    return int(stats.status_counts.get("claimed", 0))


async def _drain(queue, *, drain_timeout: float, poll_interval: float = _DRAIN_POLL_SECONDS) -> tuple[bool, int]:
    """Poll until ``claimed == 0`` or the timeout elapses. Returns
    ``(drained, last_claimed)``."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + drain_timeout
    last = -1
    consecutive_zero = 0
    while True:
        now = datetime.now(timezone.utc)
        claimed = await _claimed_count(queue, now=now)
        if claimed is None:
            return (False, -1)
        if claimed != last:
            print(f"  draining: {claimed} job(s) still claimed…")
            last = claimed
        if claimed == 0:
            consecutive_zero += 1
            if consecutive_zero >= 2:
                return (True, 0)
        else:
            consecutive_zero = 0
        if loop.time() >= deadline:
            return (False, claimed)
        await asyncio.sleep(poll_interval)


async def _run(args: argparse.Namespace) -> int:
    from kokoro_link.infrastructure.persistence.engine import (
        build_async_engine,
        build_session_factory,
    )
    from kokoro_link.infrastructure.persistence.sa_background_jobs import (
        SABackgroundJobQueue,
    )
    from kokoro_link.infrastructure.persistence.sa_background_runtime import (
        SABackgroundExecutionMode,
    )
    from kokoro_link.infrastructure.persistence.sa_background_runtime import (
        SABackgroundCoordinatorCursor,
    )
    from kokoro_link.application.services.execution_mode_transition import (
        ExecutionModeTransitionService,
    )

    database_url = _resolve_database_url(args)
    if database_url is None:
        return 2

    target = args.to
    engine = build_async_engine(database_url)
    session_factory = build_session_factory(engine)
    ownership = SABackgroundExecutionMode(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    transition_service = ExecutionModeTransitionService(
        ownership=ownership,
        queue=queue,
        cursor=SABackgroundCoordinatorCursor(session_factory),
        clock=SystemClock(),
    )
    try:
        current_mode, current_epoch = await ownership.get()
        print(f"current mode: {current_mode} (epoch {current_epoch})")
        if current_mode == target:
            print(f"already in {target} mode — nothing to do.")
            return 0

        if target == MODE_DISTRIBUTED:
            return await _to_distributed(
                ownership, queue,
                current_epoch=current_epoch, apply=args.apply,
                transition_service=transition_service,
            )
        return await _to_embedded(
            ownership, queue,
            current_epoch=current_epoch, apply=args.apply,
            drain_timeout=args.drain_timeout, poll_interval=args.poll_interval,
            transition_service=transition_service,
        )
    finally:
        await engine.dispose()


def _local_transition_service(
    ownership, queue, *, min_observation_interval: float = 1.0,
):
    return ExecutionModeTransitionService(
        ownership=ownership,
        queue=queue,
        cursor=InMemoryBackgroundCoordinatorCursor(),
        clock=SystemClock(),
        min_observation_interval=min_observation_interval,
    )


async def _to_distributed(
    ownership, queue, *, current_epoch: int, apply: bool,
    transition_service=None, poll_interval: float = _DRAIN_POLL_SECONDS,
    drain_timeout: float = 300.0,
) -> int:
    service = transition_service or _local_transition_service(
        ownership, queue, min_observation_interval=0.0,
    )
    if not apply:
        print(
            f"DRY RUN: would flip embedded → distributed "
            f"(epoch {current_epoch} → {current_epoch + 1}).",
        )
        print("Next steps after --apply:")
        print("  1. coordinator reconciles due work")
        print("  2. start worker canary (concurrency=1)")
        print("  3. verify queue/event/duplicate metrics, then scale flow")
        return 0
    entered = await service.enter_paused(expected_epoch=current_epoch)
    if not entered.ok:
        print(f"could not enter paused: {entered.code}", file=sys.stderr)
        return 1
    paused_epoch = entered.epoch
    print(f"flipped embedded → paused (epoch {paused_epoch}); verifying queue drain barrier.")
    deadline = asyncio.get_event_loop().time() + drain_timeout
    observation = await service.observe_drain(paused_epoch=paused_epoch)
    while observation.ok and not observation.confirmed:
        if asyncio.get_event_loop().time() >= deadline:
            print("drain timeout; remain paused and retry.", file=sys.stderr)
            return 1
        await asyncio.sleep(max(0.0, poll_interval))
        observation = await service.observe_drain(paused_epoch=paused_epoch)
    if not observation.ok:
        print(f"could not observe queue drain: {observation.code}", file=sys.stderr)
        return 2 if observation.code == "drain_unavailable" else 1
    left = await service.leave_paused(MODE_DISTRIBUTED, expected_epoch=paused_epoch)
    if not left.ok:
        print(f"could not leave paused: {left.code}", file=sys.stderr)
        return 1
    print(f"flipped embedded → distributed (epoch {left.epoch}).")
    print("Next: coordinator reconcile → worker canary → verify → scale.")
    return 0


async def _to_embedded(
    ownership, queue, *, current_epoch: int, apply: bool, drain_timeout: float,
    poll_interval: float = _DRAIN_POLL_SECONDS, transition_service=None,
) -> int:
    if not apply:
        print(
            f"DRY RUN: would flip distributed → embedded "
            f"(epoch {current_epoch} → {current_epoch + 1}) FIRST, then poll "
            f"the queue until claimed==0 (or --drain-timeout={drain_timeout:.0f}s).",
        )
        print("Next steps after --apply:")
        print("  1. flip mode (stops coordinator enqueue + pauses workers)")
        print("  2. drain: wait for in-flight claimed jobs to finish/expire")
        print("  3. verify no worker holds a valid lease")
        print("  4. start a single embedded scheduler")
        return 0
    service = transition_service or _local_transition_service(
        ownership, queue, min_observation_interval=0.0,
    )
    entered = await service.enter_paused(expected_epoch=current_epoch)
    if not entered.ok:
        print(f"could not enter paused: {entered.code}", file=sys.stderr)
        return 1
    paused_epoch = entered.epoch
    print(f"flipped distributed → paused (epoch {paused_epoch}).")
    deadline = asyncio.get_event_loop().time() + drain_timeout
    observation = await service.observe_drain(paused_epoch=paused_epoch)
    while observation.ok and not observation.confirmed:
        if asyncio.get_event_loop().time() >= deadline:
            print("drain timeout; remain paused and retry.", file=sys.stderr)
            return 1
        await asyncio.sleep(max(0.0, poll_interval))
        observation = await service.observe_drain(paused_epoch=paused_epoch)
    if not observation.ok:
        print(f"queue tables not readable during drain: {observation.code}", file=sys.stderr)
        return 2 if observation.code == "drain_unavailable" else 1
    left = await service.leave_paused(MODE_EMBEDDED, expected_epoch=paused_epoch)
    if not left.ok:
        print(f"could not leave paused: {left.code}", file=sys.stderr)
        return 1
    print(f"flipped paused → embedded (epoch {left.epoch}).")
    print("drain complete — 0 jobs claimed.")
    print("Next: verify no valid lease, then start ONE embedded scheduler.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Flip the background execution mode (HOSTED_CORE_SCALING §15). "
            "Dry-run by default; pass --apply to write."
        ),
    )
    parser.add_argument(
        "--to",
        required=True,
        choices=(MODE_EMBEDDED, MODE_DISTRIBUTED),
        help="Target execution mode.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually flip. Omit for a dry run (reads + prints the plan).",
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=300.0,
        help="distributed→embedded: seconds to poll for claimed==0.",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=_DRAIN_POLL_SECONDS,
        help="distributed→embedded: seconds between drain observations.",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="Target DATABASE_URL. Passing it is the prod-safety guard.",
    )
    parser.add_argument(
        "--i-know",
        action="store_true",
        help=(
            "Permit using the ambient DATABASE_URL / KOKORO_DATABASE_URL when "
            "--database-url is omitted."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
