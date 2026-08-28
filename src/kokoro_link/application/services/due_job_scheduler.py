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
* **stops the chain the same way for a dormant character** (NF4): with
  ``background_dormancy_days`` set, a character whose owner has not had a foreground
  interaction with it for that long — or has never had one at all — schedules no
  background work whatsoever. This is a *stop*, evaluated before the multiplier, not a
  fourth way to stretch the cadence, and it covers ``KnobGate.NONE`` kinds too; only
  kinds marked ``dormancy_exempt`` in the registry survive it — plus, for a character
  that has *never* been spoken to, the one kind declaring
  ``first_contact_grace_hours`` (TR2), and only until that window closes. Recovery reuses the
  thaw path exactly: a foreground turn moves ``last_active_at``, the character stops
  computing ``None``, and the next reconcile pass reseeds every missing chain.
  For a chain scheduled on behalf of more than one character (an encounter
  pair) the stop is symmetric — **any** dormant side stops it — matching the
  freeze / subscription guards around it;

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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from kokoro_link.application.services.account_runtime_profile_cache import (
    ProfileResolutionScope,
    resolve_profile_or_none,
)
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

#: ``(character, profile, now) -> is_dormant`` (NF4). Same signature as
#: :data:`IdleResolver` and reading the same foreground-interaction anchor, but
#: answering a different question — see :func:`_default_dormancy_resolver`.
DormancyResolver = Callable[
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


async def _default_dormancy_resolver(
    character: Character,
    profile: AccountRuntimeProfile,
    now: datetime,
) -> bool:
    """True when this character's background should not be scheduled at all (NF4).

    Reads the SAME foreground-interaction signal the idle down-shift reads —
    the in-memory ``character.state.last_active_at``, written at the end of every
    chat turn — so "閒置" means one thing in this module, not two.

    The one deliberate divergence from :func:`_default_idle_resolver` is the
    missing ``created_at`` fallback. Idle down-shift needs *some* anchor for a
    character that has never spoken, and creation time is the honest one there.
    Dormancy asks a different question — "has the player ever engaged this
    character?" — whose answer for a never-touched character is *no*, and
    borrowing ``created_at`` would turn that into "yes, N days ago" and run a
    full background stack for a character nobody has said a word to. Never
    interacted ⇒ dormant is the whole point of the knob, not an edge case.

    That answer is still this function's answer for every kind. The one chain
    that must survive it while the character is brand new —
    ``proactive_evaluate``, whose job is to produce the very first turn — is
    handled a level up in
    :meth:`NextDueCalculator._in_first_contact_grace`, per-kind and bounded by
    ``created_at``, precisely so this rule does not have to grow an exception
    that would then apply everywhere.

    ``None`` days (self-host, and any hosted tier that has not set the knob)
    returns ``False`` before anything else is read, so the caller's behaviour is
    unchanged.
    """
    dormancy_days = profile.background_dormancy_days
    if dormancy_days is None:
        return False
    state = getattr(character, "state", None)
    last_active = getattr(state, "last_active_at", None) if state is not None else None
    if last_active is None:
        return True
    return now - ensure_utc(last_active) >= timedelta(days=dormancy_days)


class NextDueCalculator:
    """Compute the next ``due_at`` + idempotency key for a character/kind chain."""

    def __init__(
        self,
        *,
        resolver: AccountRuntimeProfileResolverPort,
        clock: ClockPort | None = None,
        idle_resolver: IdleResolver | None = None,
        dormancy_resolver: DormancyResolver | None = None,
    ) -> None:
        self._resolver = resolver
        self._clock = clock
        self._idle_resolver = idle_resolver or _default_idle_resolver
        self._dormancy_resolver = dormancy_resolver or _default_dormancy_resolver

    def new_profile_scope(self) -> ProfileResolutionScope:
        """A memo for one caller-defined pass over many characters.

        A reconcile pass computes for every active character × every kind, and
        one operator owns many characters: without a pass-scoped memo the same
        profile is re-resolved dozens of times per pass. Passing the returned
        scope back into :meth:`compute` / :meth:`is_chain_dormant` collapses
        that to one resolution per operator per pass. Callers that handle a
        single job (the worker's handlers) do not need one — the resolver
        itself is expected to carry the steady-state TTL memo."""
        return ProfileResolutionScope(self._resolver)

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
        co_characters: Sequence[Character] = (),
        profile_scope: ProfileResolutionScope | None = None,
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

        ``co_characters`` names the OTHER characters a chain is scheduled on
        behalf of (an encounter pair's second side). The stop conditions are
        symmetric — the surrounding freeze / subscription guards already stop a
        pair when *either* side is stopped — so dormancy is evaluated for every
        subject and **any dormant subject stops the chain**. Without it the
        answer would depend on which id sorts first in the canonical pair,
        which is not a property anybody chose.

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
        profile = await self._profile_for(character, spec, profile_scope)
        # NF4: a stop, not a stretch — evaluated BEFORE the cadence math so a
        # dormant character produces no next occurrence at all, the same shape
        # as the freeze / subscription stop above. The three multiplier knobs
        # keep their meaning untouched; dormancy simply removes the chain they
        # would have scaled.
        if await self._is_dormant(character, spec, profile, resolved_now):
            return None
        # …and symmetrically for every other side the chain runs for.
        for companion in co_characters:
            companion_profile = await self._profile_for(
                companion, spec, profile_scope,
            )
            if await self._is_dormant(
                companion, spec, companion_profile, resolved_now,
            ):
                return None
        candidate = await self._candidate_due(
            character, spec, profile, resolved_now, last_due, explicit_next_due,
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
        profile: AccountRuntimeProfile | None,
        now: datetime,
        last_due: datetime | None,
        explicit_next_due: datetime | None,
    ) -> datetime:
        if explicit_next_due is not None:
            # A precisely-known instant (e.g. next beat) is honoured as-is unless it
            # is already in the past, in which case fire promptly at ``now``.
            explicit = ensure_utc(explicit_next_due)
            return explicit if explicit > now else now
        multiplier = await self._multiplier(character, spec, profile, now)
        interval = timedelta(seconds=spec.base_interval_seconds * multiplier)
        anchor = ensure_utc(last_due) if last_due is not None else now
        candidate = anchor + interval
        # Never schedule in the past: after downtime the anchored candidate may have
        # already elapsed — advance from ``now`` by one interval so the chain resumes
        # forward without back-filling missed windows.
        if candidate <= now:
            candidate = now + interval
        return candidate

    async def is_chain_dormant(
        self,
        character: Character,
        kind: str,
        *,
        now: datetime | None = None,
        co_characters: Sequence[Character] = (),
        profile_scope: ProfileResolutionScope | None = None,
    ) -> bool:
        """Whether this kind's chain must stop for a dormant subject (NF4).

        The same predicate :meth:`compute` applies before the cadence math,
        exposed so a *handler* can apply it in its pre-flight guard alongside
        freeze / subscription. Without that, a claimed job scheduled before
        the knob was set runs its whole step — a real picture, a real message
        to a player who has been gone for months — and only then discovers, at
        chain-advance time, that the character is dormant.

        Any dormant subject answers ``True`` (see ``co_characters`` on
        :meth:`compute`); a ``dormancy_exempt`` kind always answers ``False``,
        and so does a kind inside its first-contact grace for a character
        nobody has spoken to yet (:meth:`_in_first_contact_grace`) — the
        handler's pre-flight and the chain-advance stop must agree, or the
        chain would advance into a step that then refuses to run.
        """
        spec = kind_spec(kind)
        if spec is None:
            raise ValueError(f"unknown due-job kind: {kind!r}")
        if spec.dormancy_exempt:
            return False
        resolved_now = self._resolve_now(now)
        for subject in (character, *co_characters):
            profile = await self._profile_for(subject, spec, profile_scope)
            if await self._is_dormant(subject, spec, profile, resolved_now):
                return True
        return False

    async def _profile_for(
        self,
        character: Character,
        spec: KindSpec,
        scope: ProfileResolutionScope | None = None,
    ) -> AccountRuntimeProfile | None:
        """Resolve the owner's profile once per computation, when it is needed.

        Both gates below read the same profile, so resolving it here keeps one
        round-trip per ``compute`` instead of one per gate. A kind that needs
        neither — dormancy-exempt AND un-gated — still pays nothing, exactly as
        before NF4.

        ``scope`` collapses the resolutions of one whole pass (a reconcile run
        walks every character × every kind, and one operator owns many
        characters) onto a single resolution per operator; without one the
        resolver is expected to carry its own short-TTL memo, since dormancy
        made this a read on *every* computation rather than only the gated
        ones.
        """
        if spec.dormancy_exempt and spec.knob_gate is KnobGate.NONE:
            return None
        if scope is not None:
            return await scope.resolve(character.user_id)
        return await self._resolve_profile(character.user_id)

    async def _is_dormant(
        self,
        character: Character,
        spec: KindSpec,
        profile: AccountRuntimeProfile | None,
        now: datetime,
    ) -> bool:
        """Whether this kind's chain must stop for a dormant character (NF4).

        Fail-open in both directions a failure can come from: an unresolvable
        profile and a raising resolver both mean "not dormant". A control-plane
        blip must never silently switch off every hosted character's background
        — the cost of a wrong ``False`` is one extra cadence, the cost of a
        wrong ``True`` is a dead deployment that nobody gets an error about.

        The first-contact grace (TR2) is answered *here* rather than inside the
        resolver on purpose: it is a property of the **kind** — which is what
        keeps it from becoming a hole in every chain — and the resolver is
        injectable and knows nothing about kinds. Deciding it above the
        resolver also means a deployment that swaps in its own dormancy rule
        cannot accidentally lose the one chain that starts relationships.
        """
        if profile is None or spec.dormancy_exempt:
            return False
        if self._in_first_contact_grace(character, spec, now):
            return False
        try:
            return await self._dormancy_resolver(character, profile, now)
        except Exception:
            _LOGGER.exception(
                "due-job dormancy resolve failed character=%s", character.id,
            )
            return False

    @staticmethod
    def _in_first_contact_grace(
        character: Character,
        spec: KindSpec,
        now: datetime,
    ) -> bool:
        """TR2: a never-spoken-to character's bounded exemption for ONE kind.

        Three conditions, all required, none of them renewable:

        1. the kind declares ``first_contact_grace_hours`` (only
           ``proactive_evaluate`` does — the rest keep NF4 verbatim);
        2. ``last_active_at`` is still ``None``. The instant the player says
           anything, this returns ``False`` forever after and the ordinary
           "idle for N days" rule is the only one in play — the semantics of a
           character that HAS been spoken to are untouched by this method;
        3. ``now`` is still inside ``created_at + grace``. Missing
           ``created_at`` (an entity that has not round-tripped through the DB)
           answers ``False``: with no bound to compute, the conservative answer
           is the NF4 one, because an unbounded grace is the exemption this is
           specifically not.

        **Cost bound.** The grace buys ticks, not sends. Inside the window the
        proactive chain advances at its ordinary 300 s × proactive-multiplier
        cadence, and each occurrence is a cheap-band evaluation
        (:class:`ProactiveDispatcher`'s DB-only gates — enabled flags, cooldown,
        daily cap, quiet hours, and the TR2-A pre-message send cap) which only
        reaches a decider call when those all pass. TR2-A caps pre-message sends
        at 2, so once they are spent the cheap band rejects every remaining
        occurrence in the window without a provider call. Worst case for a
        character nobody ever answers is therefore: ≤72 h of indexed reads, plus
        the bounded handful of decider calls that the send cap allows — after
        which the window closes and the chain stops for good.
        """
        grace_hours = spec.first_contact_grace_hours
        if grace_hours is None:
            return False
        state = getattr(character, "state", None)
        last_active = getattr(state, "last_active_at", None) if state is not None else None
        if last_active is not None:
            return False
        created_at = getattr(character, "created_at", None)
        if created_at is None:
            return False
        return now - ensure_utc(created_at) < timedelta(hours=grace_hours)

    async def _multiplier(
        self,
        character: Character,
        spec: KindSpec,
        profile: AccountRuntimeProfile | None,
        now: datetime,
    ) -> int:
        if spec.knob_gate is KnobGate.NONE or profile is None:
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
        return await resolve_profile_or_none(self._resolver, operator_id)

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
