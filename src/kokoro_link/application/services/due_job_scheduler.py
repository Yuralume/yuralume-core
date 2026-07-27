"""Next-due computation for per-kind self-continuing jobs (HOSTED_CORE_SCALING §4.2 / §4.3).

The B1 runtime-activity gate used to be *tick-scope*: the embedded scheduler took the
whole character table each tick and multiplied the fire rate down inside the gate
(:class:`RuntimeActivityTickGate`). In the distributed due-time model there is no whole
-table tick, so §4.3 moves the three knobs
(``background_activity_multiplier`` / ``idle_downshift_days`` / ``idle_multiplier``)
into the **next-due computation**: the effective multiplier *scales the cadence* — a
2× multiplier doubles the interval, halving the cost — instead of being applied as a
post-hoc worker-side filter.

:class:`NextDueCalculator` is the single place that落點 the knob mult-in. It also:

* **stops the chain** (returns ``None``) for a frozen / subscription-locked character —
  no next job is enqueued; the reconciler reseeds after a thaw (§4.2);
* honours a **deferral seam** (``deferral_check``) so a gated occurrence (quiet hours,
  daily cap full) is *re-scheduled by rewriting ``due_at``*, never retried in place
  (§4.3.3) — the actual quiet-hours / cap math stays in the existing services and is
  delegated to, not copied (red line: 抽共用而非複製);
* mints a **logical-window idempotency key** so two racing completions computing the
  same next occurrence enqueue at most one row (the queue's active-idem index dedups).

Pure and deterministic given its inputs; the embedded scheduler never calls it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import KindSpec, KnobGate, kind_spec
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

_LOGGER = logging.getLogger(__name__)

#: ``(character, kind, candidate_due) -> pushed_due | None``. Returns a later
#: instant when the candidate occurrence is gated (quiet hours / daily cap) so the
#: caller rewrites ``due_at`` forward; ``None`` = not gated, keep the candidate.
DeferralCheck = Callable[
    [Character, str, datetime], Awaitable["datetime | None"]
]

#: ``(character, profile, now) -> is_idle``. Defaults to the state-anchor rule the
#: :class:`RuntimeActivityGateService` uses; injectable so production can wire the
#: gate's cached/repo-fallback resolver and tests can force idle on/off.
IdleResolver = Callable[
    [Character, AccountRuntimeProfile, datetime], Awaitable[bool]
]


@dataclass(frozen=True, slots=True)
class NextDue:
    """The computed next occurrence of a self-continuing chain.

    ``deferred`` records that a gate pushed ``due_at`` forward (observability only —
    the chain still advances, never retries in place)."""

    kind: str
    due_at: datetime
    idempotency_key: str
    priority: int
    capability: str
    deferred: bool = False


async def _default_idle_resolver(
    character: Character,
    profile: AccountRuntimeProfile,
    now: datetime,
) -> bool:
    """State-anchor idle rule — parity with ``RuntimeActivityGateService._is_idle``.

    Uses the in-memory ``character.state.last_active_at`` (falling back to
    ``created_at``); a character loaded for a due computation already carries state,
    so no extra repository round-trip is needed here."""
    idle_days = profile.idle_downshift_days
    if idle_days is None:
        return False
    state = getattr(character, "state", None)
    last_active = getattr(state, "last_active_at", None) if state is not None else None
    anchor = last_active if last_active is not None else getattr(character, "created_at", None)
    if anchor is None:
        return False
    return now - ensure_utc(anchor) >= timedelta(days=idle_days)


class NextDueCalculator:
    """Compute the next ``due_at`` + idempotency key for a character/kind chain."""

    def __init__(
        self,
        *,
        resolver: AccountRuntimeProfileResolverPort,
        clock: ClockPort | None = None,
        idle_resolver: IdleResolver | None = None,
    ) -> None:
        self._resolver = resolver
        self._clock = clock
        self._idle_resolver = idle_resolver or _default_idle_resolver

    async def compute(
        self,
        character: Character,
        kind: str,
        *,
        now: datetime | None = None,
        last_due: datetime | None = None,
        explicit_next_due: datetime | None = None,
        deferral_check: DeferralCheck | None = None,
        scope_id: str | None = None,
        defer_grid_seconds: float | None = None,
    ) -> NextDue | None:
        """Compute the next occurrence, or ``None`` to stop the chain.

        ``last_due`` anchors the interval-based cadence (the just-completed job's
        ``due_at``); ``explicit_next_due`` overrides it for kinds whose next instant
        is precisely known (beat due). A frozen / subscription-locked character
        returns ``None`` — the chain stops and the reconciler reseeds after a thaw.

        ``scope_id`` overrides the idempotency-key scope for a chain whose logical
        unit is not the single character (a ``pair_scoped`` encounter chain passes
        the canonical pair id). The cadence math still uses ``character`` for the
        owner's multiplier / idle state; only the key namespace changes.

        ``defer_grid_seconds`` marks a **sub-interval re-schedule** (an encounter
        gate defer pushes the pair forward by 300s — less than the 1800s base
        grid). The normal key quantises the due to the base grid on the assumption
        that consecutive occurrences are ≥1 interval apart; a 300s push violates
        that and would quantise into the SAME window as the still-``claimed``
        current occurrence, so its enqueue collides with the active-idem index and
        the chain silently breaks until the reconciler. When set, the key is minted
        in a distinct ``d``-prefixed namespace quantised to this finer grid, so it
        never collides with the base-grid current occurrence yet stays monotonic
        (each successive defer lands in the next fine window) and still dedups two
        racers computing the same defer.
        """
        spec = kind_spec(kind)
        if spec is None:
            raise ValueError(f"unknown due-job kind: {kind!r}")
        resolved_now = self._resolve_now(now)
        if getattr(character, "frozen", False) or getattr(
            character, "subscription_locked", False,
        ):
            return None
        candidate = await self._candidate_due(
            character, spec, resolved_now, last_due, explicit_next_due,
        )
        deferred = False
        if deferral_check is not None:
            try:
                pushed = await deferral_check(character, kind, candidate)
            except Exception:
                _LOGGER.exception(
                    "due-job deferral check failed kind=%s character=%s",
                    kind, character.id,
                )
                pushed = None
            if pushed is not None:
                pushed = ensure_utc(pushed)
                if pushed > candidate:
                    candidate = pushed
                    deferred = True
        return NextDue(
            kind=kind,
            due_at=candidate,
            idempotency_key=self._logical_key(
                spec, scope_id if scope_id is not None else character.id, candidate,
                defer_grid_seconds=defer_grid_seconds,
            ),
            priority=spec.priority,
            capability=spec.capability.value,
            deferred=deferred,
        )

    async def _candidate_due(
        self,
        character: Character,
        spec: KindSpec,
        now: datetime,
        last_due: datetime | None,
        explicit_next_due: datetime | None,
    ) -> datetime:
        if explicit_next_due is not None:
            # A precisely-known instant (e.g. next beat) is honoured as-is unless it
            # is already in the past, in which case fire promptly at ``now``.
            explicit = ensure_utc(explicit_next_due)
            return explicit if explicit > now else now
        multiplier = await self._multiplier(character, spec, now)
        interval = timedelta(seconds=spec.base_interval_seconds * multiplier)
        anchor = ensure_utc(last_due) if last_due is not None else now
        candidate = anchor + interval
        # Never schedule in the past: after downtime the anchored candidate may have
        # already elapsed — advance from ``now`` by one interval so the chain resumes
        # forward without back-filling missed windows.
        if candidate <= now:
            candidate = now + interval
        return candidate

    async def _multiplier(
        self, character: Character, spec: KindSpec, now: datetime,
    ) -> int:
        if spec.knob_gate is KnobGate.NONE:
            return 1
        profile = await self._resolve_profile(character.user_id)
        if profile is None:
            return 1
        try:
            idle = await self._idle_resolver(character, profile, now)
        except Exception:
            _LOGGER.exception(
                "due-job idle resolve failed character=%s", character.id,
            )
            idle = False
        if spec.knob_gate is KnobGate.PROACTIVE:
            return profile.effective_proactive_multiplier(idle=idle)
        return profile.effective_background_multiplier(idle=idle)

    async def _resolve_profile(
        self, operator_id: str,
    ) -> AccountRuntimeProfile | None:
        try:
            return await self._resolver.resolve_for_operator(operator_id)
        except Exception:
            _LOGGER.exception(
                "due-job profile resolve failed operator=%s", operator_id,
            )
            return None

    @staticmethod
    def _logical_key(
        spec: KindSpec,
        scope_id: str,
        due_at: datetime,
        *,
        defer_grid_seconds: float | None = None,
    ) -> str:
        """``kind:scope:window`` where ``scope`` is the character id (or, for a
        pair-scoped chain, the canonical pair id) and the window quantizes the due
        instant to the kind's *base* grid. The chain advances by an integer multiple
        of the base interval (multiplier ≥ 1), so consecutive occurrences land on
        distinct grid points; two racers computing the same occurrence hit the same
        window → the queue's active-idem index accepts only one.

        A sub-interval gate defer (``defer_grid_seconds`` set) instead mints
        ``kind:scope:d<window>`` quantised to the finer defer grid: the ``d``
        prefix guarantees it can never numerically collide with a base-grid window
        (so it never dedups against the still-``claimed`` current occurrence), while
        the finer grid keeps successive defers monotonic and still collapses two
        racers computing the same defer instant onto one key."""
        if defer_grid_seconds is not None:
            grid = max(1, int(defer_grid_seconds))
            window = int(due_at.timestamp()) // grid
            return f"{spec.kind}:{scope_id}:d{window}"
        grid = max(1, int(spec.base_interval_seconds))
        window = int(due_at.timestamp()) // grid
        return f"{spec.kind}:{scope_id}:{window}"

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
