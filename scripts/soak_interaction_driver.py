"""Scripted player-activity driver for the Phase 2 synthetic soak.

Drives realistic, seeded player traffic against a running soak stack so the
shadow queue sees a live work set: chat turns (which fire the post-turn /
busy-defer paths), admin freeze/unfreeze toggles (which flip the very state the
dry-run worker validates against), and the occasional manual proactive
evaluate. Every action is rate-limited, fail-soft (one bad request logs and the
loop continues), and appended to a JSONL journal with actor/target/result so a
run is fully auditable.

The action schedule is a PURE, seeded function
(:func:`build_action_schedule`) so a given ``(--seed, --duration-minutes,
--actions-per-hour)`` always produces the same sequence — the run is
reproducible and the schedule is unit-testable without any network.

Auth: with ``--auth-mode on`` the driver logs in as each seeded operator using
the credentials the seeder derived from ``(--prefix, --seed)`` (no secret is
ever passed on the command line or logged). ``--auth-mode off`` targets a
single-user / auth-disabled stack and drives the default operator's characters.

Prod-safety guard (red line): the driver performs mutating toggles, so it
refuses to run unless ``--i-know-this-is-a-soak-environment`` is passed. The
default ``--base-url`` is loopback; pointing it elsewhere still requires the
flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

# Import the seeder's deterministic helpers so credentials/emails line up
# exactly with what was seeded (single source of truth). The dual import path
# handles both direct script execution (``scripts/`` on sys.path[0]) and the
# packaged ``scripts.*`` import that pytest uses (pythonpath = ["src", "."]).
try:
    from soak_seed_population import operator_email, soak_password
except ModuleNotFoundError:  # pragma: no cover - import-context shim
    from scripts.soak_seed_population import operator_email, soak_password

_DEFAULT_BASE_URL = "http://127.0.0.1:8002"
_DEFAULT_ACTIONS_PER_HOUR = 30
_KIND_CHAT = "chat"
_KIND_FREEZE_TOGGLE = "freeze_toggle"
_KIND_PROACTIVE_EVAL = "proactive_eval"
# Subscription lock/unlock through the SAME internal Cloud→Core route Cloud
# uses (S2 landing). Conditional on an internal token — meaningful mainly in a
# cloud-mode soak where operators carry a Cloud tenant; in a local soak it still
# exercises the route mechanically. See ``--internal-token``.
_KIND_SUBSCRIPTION = "subscription_transition"
_SUBSCRIPTION_FREEZE_ROUTE = "/api/internal/v1/cloud/subscription-freeze"
_INTERNAL_TOKEN_ENV = "YURALUME_SOAK_INTERNAL_TOKEN"
_DEFAULT_WEIGHTS = {
    _KIND_CHAT: 0.7,
    _KIND_FREEZE_TOGGLE: 0.2,
    _KIND_PROACTIVE_EVAL: 0.1,
}
_SUBSCRIPTION_WEIGHT = 0.1


def resolve_weights(*, enable_subscription: bool) -> dict[str, float]:
    """The action-kind weights for a run. Adds the subscription-transition kind
    only when an internal token is supplied (so it is never scheduled — and
    never required by the health gate — without one)."""
    weights = dict(_DEFAULT_WEIGHTS)
    if enable_subscription:
        weights[_KIND_SUBSCRIPTION] = _SUBSCRIPTION_WEIGHT
    return weights
_CANNED_MESSAGES = (
    "在忙嗎？",
    "今天過得怎麼樣？",
    "剛剛想到你。",
    "hey, quick hello",
    "晚點聊，先去忙了。",
    "有推薦的音樂嗎？",
)


# --------------------------------------------------------------------------- #
# Pure action schedule
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScheduledAction:
    """One scheduled action. ``roll`` in [0, 1) selects the runtime target so
    the schedule is target-agnostic and fully deterministic."""

    offset_seconds: float
    kind: str
    roll: float


def build_action_schedule(
    *,
    seed: int,
    duration_minutes: float,
    actions_per_hour: float,
    weights: dict[str, float] | None = None,
) -> tuple[ScheduledAction, ...]:
    """Deterministic action schedule for the whole run.

    Actions are spread across the duration on an even grid with a bounded,
    seeded jitter so traffic is realistic but reproducible. Same inputs → same
    schedule (asserted by the unit tests)."""
    if duration_minutes <= 0:
        raise ValueError("--duration-minutes must be > 0")
    if actions_per_hour <= 0:
        raise ValueError("--actions-per-hour must be > 0")
    resolved_weights = weights or _DEFAULT_WEIGHTS
    kinds = tuple(resolved_weights.keys())
    weight_values = tuple(resolved_weights.values())

    duration_seconds = duration_minutes * 60.0
    total_actions = max(1, round(actions_per_hour * duration_minutes / 60.0))
    interval = duration_seconds / total_actions

    rng = random.Random(seed)
    actions: list[ScheduledAction] = []
    for i in range(total_actions):
        base = i * interval
        jitter = rng.uniform(0.0, interval * 0.5)
        offset = min(base + jitter, duration_seconds)
        kind = rng.choices(kinds, weights=weight_values, k=1)[0]
        roll = rng.random()
        actions.append(ScheduledAction(
            offset_seconds=offset, kind=kind, roll=roll,
        ))
    return tuple(actions)


# --------------------------------------------------------------------------- #
# Run health (pure — unit-testable without a network)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RunHealth:
    """Verdict on whether a soak run actually exercised the stack.

    A run is healthy only when SOMETHING was attempted, every ENABLED action
    kind reached ``min_success_per_kind`` successes, and the overall success
    ratio clears ``min_success_ratio``. An all-401/500 soak (attempted but never
    succeeded) is therefore unhealthy → non-zero exit, so a broken auth/route
    config can never masquerade as a clean comparison input."""

    attempted: Mapping[str, int]
    succeeded: Mapping[str, int]
    enabled_kinds: tuple[str, ...]
    min_success_per_kind: int
    min_success_ratio: float

    @property
    def total_attempted(self) -> int:
        return sum(self.attempted.values())

    @property
    def total_succeeded(self) -> int:
        return sum(self.succeeded.values())

    @property
    def success_ratio(self) -> float:
        return (
            self.total_succeeded / self.total_attempted
            if self.total_attempted else 0.0
        )

    @property
    def per_kind_ok(self) -> bool:
        return all(
            self.succeeded.get(kind, 0) >= self.min_success_per_kind
            for kind in self.enabled_kinds
        )

    @property
    def healthy(self) -> bool:
        return (
            self.total_attempted > 0
            and self.per_kind_ok
            and self.success_ratio >= self.min_success_ratio
        )

    def to_dict(self) -> dict:
        return {
            "kind": "run_summary",
            "attempted": dict(sorted(self.attempted.items())),
            "succeeded": dict(sorted(self.succeeded.items())),
            "enabled_kinds": list(self.enabled_kinds),
            "total_attempted": self.total_attempted,
            "total_succeeded": self.total_succeeded,
            "success_ratio": round(self.success_ratio, 4),
            "min_success_per_kind": self.min_success_per_kind,
            "min_success_ratio": self.min_success_ratio,
            "per_kind_ok": self.per_kind_ok,
            "healthy": self.healthy,
        }


# --------------------------------------------------------------------------- #
# Runtime target pool + driver
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Target:
    operator_key: str
    character_id: str
    token: str | None = None


JournalSink = Callable[[dict], None]


class SoakDriver:
    """Executes one action against the running stack, fail-soft + journaled."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        targets: Sequence[Target],
        journal: JournalSink,
        canned_messages: Sequence[str] = _CANNED_MESSAGES,
        internal_token: str = "",
        tenant_id: str = "",
    ) -> None:
        if not targets:
            raise ValueError("at least one target is required")
        self._client = client
        self._targets = tuple(targets)
        self._journal = journal
        self._messages = tuple(canned_messages)
        self._internal_token = internal_token
        self._tenant_id = tenant_id
        # Local view of freeze state so a toggle alternates freeze/unfreeze.
        self._frozen: dict[str, bool] = {}
        # Local view of subscription lock per tenant so a transition alternates.
        self._subscription_locked: dict[str, bool] = {}
        # Per-action-kind attempted / succeeded tallies for the run summary +
        # health gate (an all-401/500 soak must not report success).
        self.attempted: dict[str, int] = {}
        self.succeeded: dict[str, int] = {}

    def select(self, roll: float) -> Target:
        index = min(int(roll * len(self._targets)), len(self._targets) - 1)
        return self._targets[index]

    @staticmethod
    def _headers(target: Target) -> dict[str, str]:
        if target.token:
            return {"Authorization": f"Bearer {target.token}"}
        return {}

    async def execute(self, action: ScheduledAction) -> dict:
        target = self.select(action.roll)
        try:
            if action.kind == _KIND_CHAT:
                result = await self._do_chat(action, target)
            elif action.kind == _KIND_FREEZE_TOGGLE:
                result = await self._do_freeze_toggle(target)
            elif action.kind == _KIND_PROACTIVE_EVAL:
                result = await self._do_proactive_eval(target)
            elif action.kind == _KIND_SUBSCRIPTION:
                result = await self._do_subscription_transition(target)
            else:
                result = {"ok": False, "error": f"unknown kind {action.kind}"}
        except Exception as exc:  # noqa: BLE001 - fail-soft per action
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        # Tally attempted / succeeded per kind for the run-health gate.
        self.attempted[action.kind] = self.attempted.get(action.kind, 0) + 1
        if result.get("ok"):
            self.succeeded[action.kind] = self.succeeded.get(action.kind, 0) + 1
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "offset_seconds": round(action.offset_seconds, 3),
            "kind": action.kind,
            "actor": target.operator_key,
            "target": target.character_id,
            "result": result,
        }
        self._journal(record)
        return record

    async def _do_chat(self, action: ScheduledAction, target: Target) -> dict:
        message = self._messages[int(action.roll * len(self._messages)) % len(self._messages)]
        response = await self._client.post(
            "/api/v1/chat/messages",
            json={"character_id": target.character_id, "message": message},
            headers=self._headers(target),
        )
        return {"ok": response.status_code < 400, "status": response.status_code}

    async def _do_freeze_toggle(self, target: Target) -> dict:
        currently_frozen = self._frozen.get(target.character_id, False)
        verb = "unfreeze" if currently_frozen else "freeze"
        response = await self._client.post(
            f"/api/v1/admin/characters/{target.character_id}/{verb}",
            headers=self._headers(target),
        )
        if response.status_code < 400:
            self._frozen[target.character_id] = not currently_frozen
        return {
            "ok": response.status_code < 400,
            "status": response.status_code,
            "action": verb,
        }

    async def _do_proactive_eval(self, target: Target) -> dict:
        response = await self._client.post(
            f"/api/v1/characters/{target.character_id}/proactive/evaluate",
            headers=self._headers(target),
        )
        return {"ok": response.status_code < 400, "status": response.status_code}

    async def _do_subscription_transition(self, target: Target) -> dict:
        """Freeze / unfreeze a tenant's subscription through the internal
        Cloud→Core route (the same inbound path Cloud drives on a tier change).

        Requires an internal service token (``--internal-token`` / env); without
        one the action is a no-op that never counts as a success. The tenant is
        the configured ``--tenant-id`` or, absent that, the target operator key
        (so a local soak still exercises the route mechanically)."""
        if not self._internal_token:
            return {"ok": False, "error": "no internal token configured"}
        tenant = self._tenant_id or target.operator_key
        currently_locked = self._subscription_locked.get(tenant, False)
        verb = "unfreeze" if currently_locked else "freeze"
        response = await self._client.post(
            _SUBSCRIPTION_FREEZE_ROUTE,
            json={"tenant_id": tenant, "action": verb},
            headers={"Authorization": f"Bearer {self._internal_token}"},
        )
        if response.status_code < 400:
            self._subscription_locked[tenant] = not currently_locked
        return {
            "ok": response.status_code < 400,
            "status": response.status_code,
            "action": verb,
            "tenant_id": tenant,
        }


# --------------------------------------------------------------------------- #
# Target discovery (network)
# --------------------------------------------------------------------------- #


async def _login(
    client: httpx.AsyncClient, *, email: str, password: str,
) -> str | None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password},
    )
    if response.status_code >= 400:
        return None
    token = response.json().get("token")
    return token if isinstance(token, str) and token else None


async def _list_character_ids(
    client: httpx.AsyncClient, *, token: str | None,
) -> list[str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = await client.get("/api/v1/characters", headers=headers)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        return []
    return [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")]


async def discover_targets(
    client: httpx.AsyncClient,
    *,
    auth_mode: str,
    prefix: str,
    seed: int,
    operators: int,
) -> list[Target]:
    """Build the target pool: seeded operators' characters (auth on) or the
    default operator's characters (auth off)."""
    if auth_mode == "off":
        ids = await _list_character_ids(client, token=None)
        return [Target(operator_key="default", character_id=cid) for cid in ids]

    password = soak_password(prefix, seed)
    targets: list[Target] = []
    for i in range(operators):
        email = operator_email(prefix, i)
        token = await _login(client, email=email, password=password)
        if token is None:
            print(f"warning: login failed for {email}", file=sys.stderr)
            continue
        ids = await _list_character_ids(client, token=token)
        targets.extend(
            Target(operator_key=f"{prefix}-op{i}", character_id=cid, token=token)
            for cid in ids
        )
    return targets


# --------------------------------------------------------------------------- #
# Run loop
# --------------------------------------------------------------------------- #


async def run_schedule(
    driver: SoakDriver,
    schedule: Sequence[ScheduledAction],
    *,
    duration_seconds: float,
    stop_event: asyncio.Event,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Execute the schedule in real time, honouring duration + stop_event.

    Returns the number of actions actually executed."""
    start = monotonic()
    executed = 0
    for action in schedule:
        now = monotonic() - start
        if now >= duration_seconds or stop_event.is_set():
            break
        wait = action.offset_seconds - now
        if wait > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
                break  # stop_event fired during the wait
            except asyncio.TimeoutError:
                pass
        if stop_event.is_set() or (monotonic() - start) >= duration_seconds:
            break
        await driver.execute(action)
        executed += 1
    return executed


def _resolve_internal_token(args: argparse.Namespace) -> str:
    import os

    return (args.internal_token or os.getenv(_INTERNAL_TOKEN_ENV, "")).strip()


async def _run(args: argparse.Namespace) -> int:
    internal_token = _resolve_internal_token(args)
    weights = resolve_weights(enable_subscription=bool(internal_token))
    schedule = build_action_schedule(
        seed=args.seed,
        duration_minutes=args.duration_minutes,
        actions_per_hour=args.actions_per_hour,
        weights=weights,
    )
    duration_seconds = args.duration_minutes * 60.0

    journal_file = open(args.journal, "a", encoding="utf-8") if args.journal else None

    def _sink(record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        if journal_file is not None:
            journal_file.write(line + "\n")
            journal_file.flush()
        else:
            print(line)

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
        targets = await discover_targets(
            client,
            auth_mode=args.auth_mode,
            prefix=args.prefix,
            seed=args.seed,
            operators=args.operators,
        )
        if not targets:
            print(
                "no targets discovered — is the stack seeded and reachable?",
                file=sys.stderr,
            )
            if journal_file is not None:
                journal_file.close()
            return 2
        print(
            f"driving {len(schedule)} actions over {args.duration_minutes} min "
            f"across {len(targets)} targets"
            + (" (+subscription transitions)" if internal_token else ""),
            file=sys.stderr,
        )
        driver = SoakDriver(
            client=client, targets=targets, journal=_sink,
            internal_token=internal_token, tenant_id=args.tenant_id,
        )

        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        executed = await run_schedule(
            driver, schedule,
            duration_seconds=duration_seconds, stop_event=stop_event,
        )

    health = RunHealth(
        attempted=dict(driver.attempted),
        succeeded=dict(driver.succeeded),
        enabled_kinds=tuple(weights.keys()),
        min_success_per_kind=args.min_success_per_kind,
        min_success_ratio=args.min_success_ratio,
    )
    summary = health.to_dict()
    summary["executed"] = executed
    summary["scheduled"] = len(schedule)
    summary_line = json.dumps(summary, ensure_ascii=False)
    # Final summary JSON to stdout AND appended into the jsonl (evidence).
    print(summary_line)
    if journal_file is not None:
        journal_file.write(summary_line + "\n")
        journal_file.flush()
        journal_file.close()
    print(
        f"executed {executed}/{len(schedule)} scheduled actions; "
        f"healthy={health.healthy} ratio={health.success_ratio:.2f}",
        file=sys.stderr,
    )
    # Non-zero unless the run was healthy: an all-401/500 soak must NOT exit 0,
    # so the comparison is only trusted when the driver actually exercised the
    # stack.
    return 0 if health.healthy else 1


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, ValueError):
            # add_signal_handler is unavailable on Windows event loops; fall
            # back to the default KeyboardInterrupt propagation.
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Drive seeded player activity (chat / freeze toggle / proactive "
            "evaluate) against a running soak stack."
        ),
    )
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--duration-minutes", type=float, required=True)
    parser.add_argument(
        "--actions-per-hour", type=float, default=_DEFAULT_ACTIONS_PER_HOUR,
    )
    parser.add_argument("--seed", type=int, default=20260720)
    parser.add_argument("--prefix", default="soak")
    parser.add_argument("--operators", type=int, default=4)
    parser.add_argument(
        "--auth-mode", choices=("on", "off"), default="on",
        help="'on' logs in as seeded operators; 'off' drives the default user.",
    )
    parser.add_argument(
        "--journal", default="",
        help="JSONL path to append every action+result to (else stdout).",
    )
    parser.add_argument(
        "--min-success-per-kind", type=int, default=1,
        help=(
            "Minimum successful actions each ENABLED kind must reach for the "
            "run to be healthy (default 1). An all-401/500 soak fails this."
        ),
    )
    parser.add_argument(
        "--min-success-ratio", type=float, default=0.5,
        help=(
            "Minimum overall success ratio for a healthy run (default 0.5). "
            "Below it the driver exits non-zero and the comparison is untrusted."
        ),
    )
    parser.add_argument(
        "--internal-token", default="",
        help=(
            "Internal service bearer for the subscription-transition action "
            f"(or env {_INTERNAL_TOKEN_ENV}). Enables the "
            "subscription_transition kind that toggles a tenant's subscription "
            "lock through the internal Cloud→Core freeze route. Omit to disable "
            "that action (it is then neither scheduled nor required)."
        ),
    )
    parser.add_argument(
        "--tenant-id", default="",
        help=(
            "Cloud tenant id for subscription transitions (defaults to the "
            "target operator key). Only used when --internal-token is set."
        ),
    )
    parser.add_argument(
        "--i-know-this-is-a-soak-environment", action="store_true",
        help="Required acknowledgement — the driver mutates character state.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.i_know_this_is_a_soak_environment:
        print(
            "refusing to run: the driver mutates character freeze state. Pass "
            "--i-know-this-is-a-soak-environment to confirm the target is a "
            "soak stack.",
            file=sys.stderr,
        )
        return 2
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
