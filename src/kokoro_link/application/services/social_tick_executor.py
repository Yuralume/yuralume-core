"""Global (non-per-character) social tick body — HOSTED_CORE_SCALING §13.

Companion to :class:`CharacterTickExecutor`. Owns the steps that run once per
tick for the whole working set rather than per character: the character TTL
reaper, the idle-character freeze sweep, busy-defer follow-up release, character
encounters, peer-knowledge consolidation, and the operator-persona dream pass.

**Stateless between calls.** Every cross-tick throttle (freeze-sweep interval,
encounter-plan interval, peer-knowledge interval) lives on the caller; the caller
computes the ``sweep_freeze`` / ``plan_encounters`` / ``consolidate_peer`` flags
and updates its own timestamps from :class:`SocialTickOutcome`. Behaviour is
byte-identical to the pre-extraction scheduler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, TypeVar

from kokoro_link.application.services.character_encounter_service import (
    EncounterTickResult,
)
from kokoro_link.application.services.character_social_knowledge_service import (
    PeerKnowledgeTickResult,
)
from kokoro_link.contracts.background_activity_gate import (
    BackgroundActivityClass,
    BackgroundActivityGatePort,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

StepTimer = Callable[[str, Awaitable[_T]], Awaitable[_T]]

#: Cooperative-abort hook (P3-C), checked BETWEEN steps. ``True`` → stop the
#: remaining steps and return. Embedded scheduler passes ``None`` (byte-identical).
AbortCheck = Callable[[], bool]


def _aborted(abort_check: AbortCheck | None) -> bool:
    return abort_check is not None and abort_check()

_LOOKUP_FAILED = object()
"""Sentinel from :meth:`_get_background_character`: the repository lookup itself
crashed (transient), as opposed to ``None`` = definitively must-not-run."""


@dataclass(frozen=True, slots=True)
class SocialTickOutcome:
    """What the caller needs to advance its cross-tick throttle timestamps.

    Each flag means the throttled call ran to completion (no exception) this
    tick — the caller sets the matching ``_last_*_at`` timestamp only then,
    exactly as the embedded scheduler did before extraction."""

    encounter_planned: bool = False
    peer_consolidated: bool = False


async def _run_untimed(name: str, coro: Awaitable[_T]) -> _T:  # noqa: ARG001
    return await coro


class SocialTickExecutor:
    def __init__(
        self,
        *,
        character_ttl_reaper=None,
        character_freeze_reaper=None,
        pending_follow_up_dispatcher=None,
        character_encounter_service=None,
        character_social_knowledge_service=None,
        persona_dream_service=None,
        persona_dream_repository=None,
        character_repository=None,
        subscription_access_guard=None,
    ) -> None:
        self._character_ttl_reaper = character_ttl_reaper
        self._character_freeze_reaper = character_freeze_reaper
        self._pending_follow_up_dispatcher = pending_follow_up_dispatcher
        self._character_encounter_service = character_encounter_service
        self._character_social_knowledge_service = character_social_knowledge_service
        self._persona_dream_service = persona_dream_service
        self._persona_dream_repository = persona_dream_repository
        self._characters = character_repository
        self._subscription_access_guard = subscription_access_guard

    # The two reapers are wired AFTER construction via scheduler setters, so the
    # scheduler forwards updates here to keep the live reference current.
    def set_character_ttl_reaper(self, reaper) -> None:
        self._character_ttl_reaper = reaper

    def set_character_freeze_reaper(self, reaper) -> None:
        self._character_freeze_reaper = reaper

    # ------------------------------------------------------------------ #
    # Pre-working-set maintenance (TTL reaper always; freeze sweep gated)
    # ------------------------------------------------------------------ #
    async def run_maintenance(
        self,
        *,
        now: datetime,
        sweep_freeze: bool,
        step_timer: StepTimer | None = None,
        abort_check: AbortCheck | None = None,
    ) -> bool:
        timer = step_timer or _run_untimed
        await timer("character_ttl_reaper", self._run_character_ttl_reaper(now=now))
        if _aborted(abort_check):
            return False
        freeze_ok = await timer("freeze_sweep", self._run_freeze_sweep(now=now, sweep=sweep_freeze))
        return bool(freeze_ok)

    async def _run_character_ttl_reaper(self, *, now: datetime) -> None:
        if self._character_ttl_reaper is None:
            return
        try:
            result = await self._character_ttl_reaper.run_once(now=now)
        except Exception:
            _LOGGER.exception("proactive scheduler: character ttl reaper crashed")
            return
        if not (result.deleted_characters or result.delete_failures):
            return
        _LOGGER.info(
            "proactive scheduler: character ttl reaper scanned=%d expired=%d "
            "deleted=%d delete_failures=%d",
            result.scanned_characters,
            result.expired_characters,
            result.deleted_characters,
            result.delete_failures,
        )

    async def _run_freeze_sweep(self, *, now: datetime, sweep: bool) -> bool:
        """Run the idle-character auto-freeze sweep when the caller flags it.

        The throttle decision + timestamp update live on the caller; this only
        runs the reaper. Failure is contained — a bad sweep must not break the
        rest of the tick."""
        if self._character_freeze_reaper is None or not sweep:
            return False
        try:
            await self._character_freeze_reaper.run_once(now=now)
            return True
        except Exception:
            _LOGGER.exception("proactive scheduler: character freeze sweep crashed")
            return False

    # ------------------------------------------------------------------ #
    # Busy-defer follow-up release (global, bypasses startup grace)
    # ------------------------------------------------------------------ #
    async def run_pending_follow_ups(
        self,
        *,
        now: datetime,
        step_timer: StepTimer | None = None,
    ) -> None:
        timer = step_timer or _run_untimed
        await timer("pending_follow_ups", self._run_pending_follow_ups(now=now))

    async def _run_pending_follow_ups(self, *, now: datetime) -> None:
        if self._pending_follow_up_dispatcher is None:
            return
        try:
            await self._pending_follow_up_dispatcher.tick(now=now)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: pending_follow_up_dispatcher crashed",
            )

    # ------------------------------------------------------------------ #
    # Post-working-set social advancement (encounters / peer / dream)
    # ------------------------------------------------------------------ #
    async def run_social(
        self,
        *,
        now: datetime,
        gate: BackgroundActivityGatePort,
        plan_encounters: bool,
        consolidate_peer: bool,
        step_timer: StepTimer | None = None,
        abort_check: AbortCheck | None = None,
    ) -> SocialTickOutcome:
        timer = step_timer or _run_untimed
        encounter_planned = await timer(
            "encounters",
            self._run_encounters(
                now=now, gate=gate, plan_encounters=plan_encounters,
            ),
        )
        if _aborted(abort_check):
            return SocialTickOutcome(encounter_planned=encounter_planned)
        peer_consolidated = await timer(
            "peer_knowledge",
            self._run_peer_knowledge(
                now=now, gate=gate, consolidate=consolidate_peer,
            ),
        )
        if _aborted(abort_check):
            return SocialTickOutcome(
                encounter_planned=encounter_planned,
                peer_consolidated=peer_consolidated,
            )
        await timer("persona_dream", self._run_persona_dream(now=now, gate=gate))
        return SocialTickOutcome(
            encounter_planned=encounter_planned,
            peer_consolidated=peer_consolidated,
        )

    async def _run_encounters(
        self,
        *,
        now: datetime,
        gate: BackgroundActivityGatePort,
        plan_encounters: bool,
    ) -> bool:
        """Run due encounters (always) + plan new ones when flagged.

        Returns whether planning ran to completion so the caller can advance its
        ``_last_encounter_plan_at`` throttle — exactly the point the embedded
        scheduler updated the timestamp (right after ``plan_pending`` returned)."""
        if self._character_encounter_service is None:
            return False
        try:
            run_result = await self._character_encounter_service.run_pending(
                now=now, gate=gate,
            )
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: character encounter run crashed",
            )
            run_result = None

        plan_result: EncounterTickResult | None = None
        planned = False
        if plan_encounters:
            try:
                plan_result = await self._character_encounter_service.plan_pending(
                    now=now, gate=gate,
                )
                planned = True
            except Exception:
                _LOGGER.exception(
                    "proactive scheduler: character encounter plan crashed",
                )
        self._log_encounter_tick_result("run", run_result)
        self._log_encounter_tick_result("plan", plan_result)
        return planned

    async def _run_peer_knowledge(
        self,
        *,
        now: datetime,  # noqa: ARG002 — kept for call-shape symmetry with caller
        gate: BackgroundActivityGatePort,
        consolidate: bool,
    ) -> bool:
        if self._character_social_knowledge_service is None:
            return False
        if not consolidate:
            return False
        try:
            result = (
                await self._character_social_knowledge_service.consolidate_due(
                    gate=gate,
                )
            )
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: peer knowledge consolidation crashed",
            )
            return False
        self._log_peer_knowledge_tick_result(result)
        return True

    async def _run_persona_dream(
        self,
        *,
        now: datetime,
        gate: BackgroundActivityGatePort,
    ) -> None:
        """Operator-persona dream pass — per (character, operator).

        Each character's persona accumulates independently, so each gets its own
        dream pass with its own quiet-hours / pending / interval gate. Failure is
        contained — a crashed pass on one pair must not affect the next."""
        if (
            self._persona_dream_service is None
            or self._persona_dream_repository is None
        ):
            return
        try:
            pairs = await self._persona_dream_repository.list_characters_with_pending()
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: persona dream pair lookup crashed",
            )
            return
        for character_id, operator_id in pairs:
            # A frozen character halts all background consolidation. Cheap
            # per-pair guard — the pending-staging list is small.
            character = await self._get_background_character(character_id)
            if character is None:
                continue
            if character is _LOOKUP_FAILED:
                # Fail-open: a transient lookup error must not suppress the dream
                # pass (the service's own gates still apply); the cost gate is
                # skipped this round because there is no entity to evaluate.
                pass
            elif not await gate.allows(
                character, BackgroundActivityClass.PERSONA_DREAM,
            ):
                continue
            try:
                should_run = await self._persona_dream_service.should_run_now(
                    character_id, operator_id, now=now,
                )
            except Exception:
                _LOGGER.exception(
                    "proactive scheduler: persona dream should_run_now "
                    "crashed char=%s op=%s", character_id, operator_id,
                )
                continue
            if not should_run:
                continue
            try:
                await self._persona_dream_service.run_consolidation(
                    character_id, operator_id, now=now,
                )
            except Exception:
                _LOGGER.exception(
                    "proactive scheduler: persona dream consolidation "
                    "crashed char=%s op=%s", character_id, operator_id,
                )

    # -- Phase 5 §13 per-kind social step entry points -------------------- #
    #
    # The cross-character social_tick is split into per-target due chains (§13).
    # ``persona_dream`` and ``peer_knowledge`` are per-character kinds; each public
    # step delegates to the SAME single-character service call the whole-tick body
    # used (抽共用而非複製), running that character's own background mutation with
    # the service's own quiet-hours / pending / freeze guards. The B1 background
    # multiplier was already spent scaling the chain cadence at due time (§4.3.1),
    # so the step does NOT re-apply the cost gate — exactly like ``step_feed_compose``.
    # (``encounter_tick`` is pair-scoped and lives on the CharacterEncounterService
    # step under a pair lease, not here.)

    async def step_persona_dream(
        self, character, *, now: datetime,
    ) -> bool:
        """``persona_dream`` kind: one dream pass for this character's owner-operator.

        A character has exactly one owner, so the (character, operator) dream unit
        is (character.id, character.user_id). Runs the service's own
        ``should_run_now`` gate (quiet hours + min interval + pending count) before
        the LLM consolidation. Returns whether a consolidation ran."""
        if self._persona_dream_service is None:
            return False
        operator_id = getattr(character, "user_id", None)
        if not operator_id:
            return False
        try:
            should_run = await self._persona_dream_service.should_run_now(
                character.id, operator_id, now=now,
            )
        except Exception:
            _LOGGER.exception(
                "persona dream should_run_now crashed char=%s op=%s",
                character.id, operator_id,
            )
            return False
        if not should_run:
            return False
        try:
            await self._persona_dream_service.run_consolidation(
                character.id, operator_id, now=now,
            )
        except Exception:
            _LOGGER.exception(
                "persona dream consolidation crashed char=%s op=%s",
                character.id, operator_id,
            )
            return False
        return True

    async def step_peer_knowledge(self, character) -> bool:  # noqa: ANN001
        """``peer_knowledge`` kind: consolidate this character's peer profiles.

        Delegates to :meth:`CharacterSocialKnowledgeService.consolidate_for` — this
        character's side of every enabled relationship. The peer's side is owned by
        the peer's own chain, so there is no cross-character fan-out. Returns whether
        anything was consolidated."""
        if self._character_social_knowledge_service is None:
            return False
        try:
            result = await self._character_social_knowledge_service.consolidate_for(
                character.id,
            )
        except Exception:
            _LOGGER.exception(
                "peer knowledge consolidation crashed character=%s", character.id,
            )
            return False
        return result.consolidated > 0

    async def _get_background_character(self, character_id: str):
        """Load one cross-character tick target.

        Returns the character when it may do background work, ``None`` when it
        definitively must not (missing / frozen / subscription-locked), and
        :data:`_LOOKUP_FAILED` when the repository lookup itself crashed. Callers
        treat lookup failure as **fail-open** — a transient repo error must never
        silently suppress legitimate work; only the cost gate is skipped for that
        round because there is no entity to evaluate."""
        if self._characters is None:
            return None
        try:
            character = await self._characters.get(character_id)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: background lookup failed character=%s",
                character_id,
            )
            return _LOOKUP_FAILED
        if character is None or character.frozen or character.subscription_locked:
            return None
        if not await self._subscription_allows(character):
            return None
        return character

    async def subscription_allows(self, character) -> bool:  # noqa: ANN001
        """Fail-soft single-character subscription recheck (public).

        The per-kind social handlers (§13 split) run the same access check as the
        whole-tick body's pre-loop filter before advancing a chain."""
        return await self._subscription_allows(character)

    async def _subscription_allows(self, character) -> bool:
        if self._subscription_access_guard is None:
            return True
        try:
            return await self._subscription_access_guard.is_character_allowed(
                character,
            )
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: subscription guard failed character=%s",
                character.id,
            )
            return False

    def _log_encounter_tick_result(
        self,
        phase: str,
        result: EncounterTickResult | None,
    ) -> None:
        if result is None or not (
            result.planned or result.completed or result.failed
        ):
            return
        _LOGGER.info(
            "proactive scheduler: character encounter %s planned=%d "
            "completed=%d failed=%d planned_ids=%s completed_ids=%s failed_ids=%s",
            phase,
            result.planned,
            result.completed,
            result.failed,
            _short_ids(result.planned_ids),
            _short_ids(result.completed_ids),
            _short_ids(result.failed_ids),
        )

    def _log_peer_knowledge_tick_result(
        self,
        result: PeerKnowledgeTickResult,
    ) -> None:
        if not (result.consolidated or result.failed):
            return
        _LOGGER.info(
            "proactive scheduler: peer knowledge consolidated=%d skipped=%d "
            "failed=%d pairs=%s",
            result.consolidated,
            result.skipped,
            result.failed,
            _short_ids(tuple(f"{a}->{b}" for a, b in result.consolidated_pairs)),
        )


def _short_ids(ids: tuple[str, ...], *, limit: int = 5) -> tuple[str, ...]:
    visible = tuple(item[:12] for item in ids[:limit])
    if len(ids) <= limit:
        return visible
    return (*visible, f"+{len(ids) - limit}")
