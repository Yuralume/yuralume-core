"""The dormant LINE-bound characters an operator may call back (LR T1, D1).

Core is the only place this list can be produced. Cloud's channel rows
carry no activity signal at all (``last_inbound_at`` / ``last_outbound_at``
are dead columns), and Core itself had only aggregate activity statistics
— no query that answers "which characters are dormant right now".

The filter is D1, and it runs as four *stages* rather than a walk over
characters. The staging is the point: every question this listing asks is
either free (already in memory) or answered **per operator**, and only the
last one costs anything per character.

1. **Not frozen, not subscription-locked, and has ever interacted** —
   pure, from the rows ``list_active()`` already returned. A character
   nobody has spoken to is not "dormant", it is unstarted, and pushing to
   one is spam the first-contact gate would refuse anyway.
2. **Tier dormancy window** — one control-plane read per *distinct
   operator*, not per character. A single owner commonly holds a whole
   roster and the answer is one row.
3. **Past that window** — the exact predicate the background scheduler
   already uses, reused rather than restated (see
   :func:`.dormancy.is_dormant_by_scheduler_rule`). Pure: it reads the
   character state already in memory.
4. **Has a cloud identity projection** — one operator read per distinct
   *surviving* operator, evaluated through
   :func:`~kokoro_link.application.services.proactive_delivery.hosted_identity.cloud_identity_of`
   — the same rule the hosted delivery path applies, so a character that
   would be skipped at send time never appears as selectable here.
5. **Channel eligibility** — a bounded fan-out of the non-authoritative
   cost preflight. This one does *not* filter: an ineligible character is
   still listed, with its reason, because "why can't I pick this one?" is
   a question the console must be able to answer.

Two properties this shape buys, both of which the console's single
``GET`` depends on:

*Per-operator, not per-character.* The earlier walk asked the hosted
identity resolver one character at a time, and that resolver re-reads the
character (already in hand) and then its operator — two round trips per
dormant row, for an answer that is identical for every character the same
person owns. Stages 2 and 4 fan out over *distinct* operator ids, so the
de-duplication is structural: no memo can be defeated by concurrency,
because a given id appears exactly once in the fan-out.

*Bounded, and time-boxed.* Every stage that awaits anything runs through
:meth:`~LineReactivationCandidateService._map_concurrently` — one
``asyncio.gather`` under a shared concurrency ceiling — and the whole call
carries a wall-clock budget. A channel that has gone slow costs the
operator a page of ``transient_error`` rows, never a request that hangs
past the caller's own timeout.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar

from kokoro_link.application.services.account_runtime_profile_cache import (
    resolve_profile_or_none,
)
# The scheduler's own dormancy predicate, behind the package's shared
# seam rather than restated here. "Dormant" has to mean one thing across
# the deployment: if this listing used a second copy of the rule, a
# tuning change to the scheduler would silently start offering characters
# whose background is still running (or hiding ones whose background
# already stopped), and nothing would turn red. The campaign runner
# re-asks the same question at send time through the same seam.
from kokoro_link.application.services.line_reactivation.dormancy import (
    is_dormant_by_scheduler_rule as _is_dormant_by_scheduler_rule,
)
# The hosted delivery path's own projection rule, imported rather than
# restated for the same reason — and in its operator-shaped form, so this
# listing never re-reads a character it already holds.
from kokoro_link.application.services.proactive_delivery.hosted_identity import (
    cloud_identity_of,
    owning_operator_id,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.external_proactive import (
    ExternalProactiveDeliveryPort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

_LOGGER = logging.getLogger(__name__)

_KeyT = TypeVar("_KeyT")
_ValueT = TypeVar("_ValueT")

DEFAULT_ELIGIBILITY_CONCURRENCY = 8
"""Simultaneous awaits in any one stage (plan §2). Sized for the
eligibility probe, which is the expensive stage: a cheap hint endpoint,
but still one HTTP call per candidate, and the console asks for the whole
list at once. Eight keeps a several-hundred character roster responsive
without turning an admin page load into a burst the channel — or, in the
per-operator stages, the database pool — has to absorb."""

DEFAULT_LISTING_BUDGET_SECONDS = 60.0
"""Wall-clock budget for one ``list_candidates`` call.

The console's ``GET`` is a synchronous request behind a proxy with its own
timeout; a listing that runs past it returns nothing at all, which is the
one outcome worse than an incomplete page. So the budget is enforced from
the inside: probes that have not started once it is spent answer
``transient_error`` immediately (an honest "unknown, so not selectable"),
and the operator gets the rest of the page.

Checked *before* each await rather than by cancelling work in flight —
tearing down a pooled database read mid-query costs more than the wait it
saves — with the sole exception of the eligibility probe, which is a
per-character HTTP call that nothing else shares and is therefore safe to
cut off."""

ELIGIBILITY_REASON_TRANSIENT = "transient_error"
"""The probe itself failed, or the listing budget ran out before it could
run. Rendered as not-eligible so the operator does not select something
that cannot be sent, but named distinctly from a real negative ("no
active endpoint") so a channel outage is not misread as every player
having unbound LINE."""

_DEFAULT_PROFILE_NAME = "default"
"""``AccountRuntimeProfile.name`` for the permissive fall-back profile.
Unreachable for a listed candidate — a candidate must have a non-``None``
``background_dormancy_days``, which only a control-plane tier profile
carries — but mapped to ``tier_key=None`` rather than reported as a tier
that does not exist."""


@dataclass(frozen=True, slots=True)
class ReactivationCandidate:
    """One dormant character, as the console renders it."""

    character_id: str
    character_name: str
    user_id: str
    tier_key: str | None
    last_active_at: datetime
    dormancy_days: int
    """The tier's configured window, in days."""

    dormant_for_days: int
    """Whole days since ``last_active_at``. Always ``>= dormancy_days``."""

    eligible: bool
    eligibility_reason: str | None


@dataclass(frozen=True, slots=True)
class ReactivationCandidateList:
    """The whole answer, with the instant it describes.

    ``generated_at`` is not decoration: dormancy is a moving target and
    the console shows this list for as long as the operator takes to make
    a selection.
    """

    generated_at: datetime
    candidates: tuple[ReactivationCandidate, ...]


@dataclass(frozen=True, slots=True)
class _DormantCharacter:
    """A character that cleared D1 steps 1–3, awaiting identity and probe."""

    character: Character
    profile: AccountRuntimeProfile
    last_active_at: datetime
    dormancy_days: int
    dormant_for_days: int


class LineReactivationCandidateService:
    """Produce the dormant-candidate list for the admin console."""

    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        operator_repository: OperatorProfileRepositoryPort,
        profile_resolver: AccountRuntimeProfileResolverPort,
        external_delivery: ExternalProactiveDeliveryPort,
        clock: ClockPort | None = None,
        concurrency: int = DEFAULT_ELIGIBILITY_CONCURRENCY,
        budget_seconds: float | None = DEFAULT_LISTING_BUDGET_SECONDS,
    ) -> None:
        self._characters = character_repository
        self._operators = operator_repository
        self._profile_resolver = profile_resolver
        self._delivery = external_delivery
        self._clock = clock
        self._concurrency = max(1, int(concurrency))
        self._budget_seconds = (
            None if budget_seconds is None else max(0.0, float(budget_seconds))
        )

    async def list_candidates(
        self, *, now: datetime | None = None,
    ) -> ReactivationCandidateList:
        moment = self._resolve_now(now)
        deadline = self._open_budget()
        engaged = _engaged(await self._characters.list_active())
        profiles = await self._resolve_profiles(_operators_of(engaged), deadline)
        dormant = await self._select_dormant(engaged, profiles, moment)
        identities = await self._resolve_identities(
            _operators_of(entry.character for entry in dormant), deadline,
        )
        reachable = tuple(
            entry
            for entry in dormant
            if identities.get(owning_operator_id(entry.character)) is not None
        )
        eligibility = await self._probe_eligibility(
            tuple(entry.character.id for entry in reachable), deadline,
        )
        candidates = tuple(
            _to_candidate(entry, *eligibility[entry.character.id])
            for entry in reachable
        )
        # Longest dormant first: the console's default selection order is
        # "who has been gone longest", and that is also the order in which
        # a partially-selected campaign does the most good.
        return ReactivationCandidateList(
            generated_at=moment,
            candidates=tuple(
                sorted(
                    candidates,
                    key=lambda item: (-item.dormant_for_days, item.character_id),
                ),
            ),
        )

    async def _resolve_profiles(
        self, operator_ids: Sequence[str], deadline: float | None,
    ) -> dict[str, AccountRuntimeProfile | None]:
        """One control-plane read per operator, fail-open to ``None``.

        A budget that expires here answers ``None``, which is the same
        answer a control-plane failure gives and which this listing
        already reads as "no dormancy policy known, so not offerable" —
        an under-filled page, never a wrong row.
        """

        async def resolve(operator_id: str) -> AccountRuntimeProfile | None:
            if _expired(deadline):
                _LOGGER.warning(
                    "line reactivation listing budget spent before resolving "
                    "tier operator=%s",
                    operator_id,
                )
                return None
            return await resolve_profile_or_none(
                self._profile_resolver, operator_id,
            )

        return await self._map_concurrently(operator_ids, resolve)

    async def _select_dormant(
        self,
        characters: Sequence[Character],
        profiles: dict[str, AccountRuntimeProfile | None],
        moment: datetime,
    ) -> tuple[_DormantCharacter, ...]:
        """Apply the scheduler's dormancy rule — pure, so a plain walk.

        Nothing here awaits the outside world: the profile is already
        resolved and the predicate reads only in-memory character state.
        Keeping it serial keeps the input order, which the final sort then
        refines rather than replaces.
        """

        collected: list[_DormantCharacter] = []
        for character in characters:
            profile = profiles.get(owning_operator_id(character))
            if profile is None:
                continue
            dormancy_days = profile.background_dormancy_days
            if dormancy_days is None:
                continue
            if not await _is_dormant_by_scheduler_rule(character, profile, moment):
                continue
            last_active = ensure_utc(_last_active_of(character))
            collected.append(
                _DormantCharacter(
                    character=character,
                    profile=profile,
                    last_active_at=last_active,
                    dormancy_days=dormancy_days,
                    dormant_for_days=max(0, (moment - last_active).days),
                ),
            )
        return tuple(collected)

    async def _resolve_identities(
        self, operator_ids: Sequence[str], deadline: float | None,
    ) -> dict[str, "tuple[str, str] | None"]:
        """One operator read per operator; ``None`` = no hosted destination.

        Fail-closed on every unknown, budget included: parity with
        delivery means a row is only offered when its projection was
        actually seen.
        """

        async def resolve(operator_id: str) -> "tuple[str, str] | None":
            if _expired(deadline):
                _LOGGER.warning(
                    "line reactivation listing budget spent before resolving "
                    "cloud identity operator=%s",
                    operator_id,
                )
                return None
            try:
                operator = await self._operators.get(operator_id)
            except Exception:
                _LOGGER.warning(
                    "line reactivation identity read failed operator=%s",
                    operator_id,
                    exc_info=True,
                )
                return None
            return cloud_identity_of(operator)

        return await self._map_concurrently(operator_ids, resolve)

    async def _probe_eligibility(
        self, character_ids: Sequence[str], deadline: float | None,
    ) -> dict[str, tuple[bool, str | None]]:
        async def probe(character_id: str) -> tuple[bool, str | None]:
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0.0:
                # The budget is spent and this row never got its turn.
                # Reported exactly like a failed probe, because from the
                # operator's side it is one: nobody asked the channel.
                _LOGGER.warning(
                    "line reactivation listing budget spent before probing "
                    "character=%s",
                    character_id,
                )
                return (False, ELIGIBILITY_REASON_TRANSIENT)
            try:
                verdict = await _within(
                    self._delivery.check_eligibility(character_id), remaining,
                )
            except Exception:
                # One character's failed probe must not cost the
                # operator the whole list. The adapter already
                # absorbs its own typed transient error; anything
                # reaching here is an outage, a spent budget or a
                # defect, and either way the honest answer for this
                # row is "unknown, so not selectable".
                _LOGGER.warning(
                    "line reactivation eligibility probe failed character=%s",
                    character_id,
                    exc_info=True,
                )
                return (False, ELIGIBILITY_REASON_TRANSIENT)
            return (
                verdict.eligible,
                None if verdict.eligible else (verdict.reason or "ineligible"),
            )

        return await self._map_concurrently(character_ids, probe)

    async def _map_concurrently(
        self,
        keys: Sequence[_KeyT],
        worker: Callable[[_KeyT], Awaitable[_ValueT]],
    ) -> dict[_KeyT, _ValueT]:
        """Run ``worker`` over distinct ``keys``, at most N at a time.

        The one fan-out primitive of this module, so the concurrency
        ceiling is stated once and every stage — control-plane reads,
        operator reads, channel probes — is bounded by the same number.
        Callers pass de-duplicated keys, which is also what makes the
        result safe to index by key.
        """

        if not keys:
            return {}
        semaphore = asyncio.Semaphore(self._concurrency)

        async def run(key: _KeyT) -> _ValueT:
            async with semaphore:
                return await worker(key)

        results = await asyncio.gather(*(run(key) for key in keys))
        return dict(zip(keys, results, strict=True))

    def _open_budget(self) -> float | None:
        """Deadline on the event loop's monotonic clock, or ``None``.

        Deliberately not ``ClockPort``: a wall-clock jump must not be able
        to end (or extend) an in-flight listing, and this budget is never
        persisted or reported — it only ever gates awaits in this call.
        """

        if self._budget_seconds is None:
            return None
        return asyncio.get_running_loop().time() + self._budget_seconds

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


def _engaged(characters: Sequence[Character]) -> tuple[Character, ...]:
    """D1 steps 1–2, both free: the site locks and "has ever interacted".

    The locks are re-asserted here even though ``list_active()`` supplies
    them, so this service does not silently depend on a repository detail
    it does not own.
    """

    return tuple(
        character
        for character in characters
        if not getattr(character, "frozen", False)
        and not getattr(character, "subscription_locked", False)
        and _last_active_of(character) is not None
    )


def _last_active_of(character: Character) -> datetime | None:
    state = getattr(character, "state", None)
    if state is None:
        return None
    return getattr(state, "last_active_at", None)


def _operators_of(characters) -> tuple[str, ...]:  # noqa: ANN001 - any iterable
    """Distinct owning operator ids, in first-seen order.

    First-seen order rather than a set, so a fan-out over these keys is
    reproducible between runs — a listing that reorders its own database
    reads is needlessly hard to read in a slow-query log.
    """

    return tuple(
        dict.fromkeys(owning_operator_id(character) for character in characters),
    )


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - asyncio.get_running_loop().time()


def _expired(deadline: float | None) -> bool:
    remaining = _remaining(deadline)
    return remaining is not None and remaining <= 0.0


async def _within(awaitable: Awaitable[_ValueT], timeout: float | None) -> _ValueT:
    if timeout is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout)


def _to_candidate(
    entry: _DormantCharacter, eligible: bool, reason: str | None,
) -> ReactivationCandidate:
    tier_key = entry.profile.name
    return ReactivationCandidate(
        character_id=entry.character.id,
        character_name=entry.character.name,
        user_id=entry.character.user_id,
        tier_key=None if tier_key == _DEFAULT_PROFILE_NAME else tier_key,
        last_active_at=entry.last_active_at,
        dormancy_days=entry.dormancy_days,
        dormant_for_days=entry.dormant_for_days,
        eligible=eligible,
        eligibility_reason=reason,
    )


__all__ = [
    "DEFAULT_ELIGIBILITY_CONCURRENCY",
    "DEFAULT_LISTING_BUDGET_SECONDS",
    "ELIGIBILITY_REASON_TRANSIENT",
    "LineReactivationCandidateService",
    "ReactivationCandidate",
    "ReactivationCandidateList",
]
