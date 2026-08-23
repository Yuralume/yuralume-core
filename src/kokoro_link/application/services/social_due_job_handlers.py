"""Per-kind handler dispatch + self-chain advance for the social split (§13).

Companion to :class:`CharacterKindHandler`. That handler owns the per-character
``character_tick`` split; this one owns the cross-character ``social_tick`` split:

* ``persona_dream`` / ``peer_knowledge`` — ordinary character-scoped chains. After
  the shared pre-flight guard (existence / frozen / subscription) the bound
  SocialTickExecutor step runs and the self-chain advances by the kind's cadence,
  exactly like the character kinds.
* ``encounter_tick`` — a PAIR chain. Its scope key is the canonical pair id
  (relationship id); its handler takes an atomic two-character lease
  (:class:`EncounterPairLeaseSession`) BEFORE any LLM (§6.1):

  - lease contended (a crossing pair holds one character) → do NOT run; advance the
    chain to the next cadence so the pair retries once the crossing encounter frees
    the character (deadlock-free: acquire always locks rows in canonical order);
  - §4.3.2 both-side AND gate AFTER the lease, BEFORE the LLM: if either side is
    off-phase / idle-suppressed, release and re-schedule the pair forward by
    ``_GATE_DEFER_SECONDS`` (rewrite due, never retry in place, never hold a slot);
  - lease lost mid-run → ABANDON without finalizing (LEASE_ABANDONED): the
    reclaiming worker owns the pair and the encounter CAS / memory dedup / visible
    -slot ledgers protect anything already emitted.

Two invariants carried from Phase 3 (§3.3, red line), identical to the character
handler: a stopped character (frozen / locked / dormant / disabled relationship)
runs no step AND breaks its chain (the reconciler reseeds after a thaw or the
player's next foreground interaction); the B1 multiplier is
spent scaling the cadence at due time, so the step does not re-filter — except the
encounter §4.3.2 both-side gate, which is the pair analogue of the character kinds'
already-spent multiplier (idle is per-character, so both sides must be on-phase).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.character_encounter_service import (
    _GATE_DEFER_SECONDS,
    CharacterEncounterService,
)
from kokoro_link.application.services.due_job_reconciler import EpochProvider
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.application.services.encounter_pair_lease import (
    EncounterPairLeaseLost,
    EncounterPairLeaseSession,
    _DEFAULT_LEASE_TTL_SECONDS,
)
from kokoro_link.application.services.social_tick_executor import SocialTickExecutor
from kokoro_link.contracts.background_activity_gate import (
    BackgroundActivityClass,
    BackgroundActivityGatePort,
)
from kokoro_link.contracts.background_jobs import (
    BackgroundJobQueuePort,
    BackgroundJobSpec,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import (
    ENCOUNTER_TICK_KIND,
    PEER_KNOWLEDGE_KIND,
    PERSONA_DREAM_KIND,
    is_pair_chain_kind,
    kind_spec,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.pair_lease import PairLeasePort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_relationship import CharacterRelationship

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SocialHandlerResult:
    """Outcome of handling one social per-kind job."""

    kind: str
    executed: bool
    chain_stopped: bool = False
    next_enqueued: bool = False
    #: Encounter only: a crossing pair held one character; the run was skipped and
    #: the chain advanced to the next cadence (not deferred, not abandoned).
    lease_contended: bool = False
    #: Encounter only: the §4.3.2 both-side gate denied; the pair was re-scheduled
    #: forward by ``_GATE_DEFER_SECONDS`` instead of running.
    deferred: bool = False
    #: Encounter only: the pair lease was lost mid-run; the job must be ABANDONED
    #: (not completed) so the reclaiming worker owns the pair.
    abandoned: bool = False


class SocialKindHandler:
    """Runs a social kind's step and advances its self-chain (§13)."""

    def __init__(
        self,
        *,
        social_executor: SocialTickExecutor,
        encounter_service: CharacterEncounterService,
        queue: BackgroundJobQueuePort,
        next_due_calculator: NextDueCalculator,
        epoch_provider: EpochProvider,
        pair_lease: PairLeasePort | None = None,
        pair_lease_owner: str = "social-worker",
        pair_lease_ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS,
        pair_lease_heartbeat_interval_seconds: float | None = None,
        operator_profile_repository: OperatorProfileRepositoryPort | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._social = social_executor
        self._encounters = encounter_service
        self._queue = queue
        self._calculator = next_due_calculator
        self._epoch_provider = epoch_provider
        self._pair_lease = pair_lease
        self._pair_lease_owner = pair_lease_owner
        self._pair_lease_ttl = pair_lease_ttl_seconds
        self._pair_lease_hb = pair_lease_heartbeat_interval_seconds
        self._operator_profiles = operator_profile_repository
        self._clock = clock

    # -- character-scoped kinds (persona_dream / peer_knowledge) ----------- #

    async def handle_character(
        self,
        character: Character,
        kind: str,
        *,
        now: datetime | None = None,
        last_due: datetime | None = None,
        fencing_epoch: int | None = None,
    ) -> SocialHandlerResult:
        if is_pair_chain_kind(kind):  # pragma: no cover — routing guard
            raise ValueError(f"{kind!r} is pair-scoped; use handle_pair")
        if kind_spec(kind) is None:
            raise ValueError(f"unknown social due-job kind: {kind!r}")
        resolved = self._resolve_now(now)
        # Shared pre-flight guard (§3.3): a stopped character runs no step and
        # breaks its chain — the reconciler reseeds after thaw.
        if character.frozen or character.subscription_locked:
            return SocialHandlerResult(kind, executed=False, chain_stopped=True)
        if not await self._social.subscription_allows(character):
            return SocialHandlerResult(kind, executed=False, chain_stopped=True)
        # NF4: dormancy joins the pre-flight for the same reason as freeze —
        # an already-queued job must not spend an LLM call on a character
        # nobody has spoken to and only then break its chain.
        if await self._calculator.is_chain_dormant(character, kind, now=resolved):
            return SocialHandlerResult(kind, executed=False, chain_stopped=True)
        if kind == PERSONA_DREAM_KIND:
            await self._social.step_persona_dream(character, now=resolved)
        elif kind == PEER_KNOWLEDGE_KIND:
            await self._social.step_peer_knowledge(character)
        else:  # pragma: no cover — registry / dispatch kept in lockstep
            raise ValueError(f"no character step bound for social kind: {kind!r}")
        chained = await self._advance_chain(
            character, kind, resolved, character.id, last_due, fencing_epoch,
        )
        return SocialHandlerResult(kind, executed=True, next_enqueued=chained)

    # -- pair-scoped kind (encounter_tick) --------------------------------- #

    async def handle_pair(
        self,
        char_a: Character,
        char_b: Character,
        relationship: CharacterRelationship,
        *,
        now: datetime | None = None,
        last_due: datetime | None = None,
        fencing_epoch: int | None = None,
        gate: BackgroundActivityGatePort | None = None,
    ) -> SocialHandlerResult:
        resolved = self._resolve_now(now)
        kind = ENCOUNTER_TICK_KIND
        # Pre-flight: a disabled relationship or a stopped character on either side
        # breaks the pair chain (reconciler reseeds on re-enable / thaw).
        if not relationship.enabled:
            return SocialHandlerResult(kind, executed=False, chain_stopped=True)
        for character in (char_a, char_b):
            if character.frozen or character.subscription_locked:
                return SocialHandlerResult(kind, executed=False, chain_stopped=True)
            if not await self._social.subscription_allows(character):
                return SocialHandlerResult(kind, executed=False, chain_stopped=True)
        # NF4, both sides — the same "either side stops the pair" shape as the
        # two guards above, and evaluated in the same place so a queued
        # encounter never takes the pair lease and runs the model for a pair
        # whose player is gone.
        if await self._calculator.is_chain_dormant(
            char_a, kind, now=resolved, co_characters=(char_b,),
        ):
            return SocialHandlerResult(kind, executed=False, chain_stopped=True)

        contended = gated = abandoned = executed = False
        async with EncounterPairLeaseSession(
            self._pair_lease, char_a.id, char_b.id,
            owner=self._pair_lease_owner,
            ttl_seconds=self._pair_lease_ttl,
            clock=self._clock,
            heartbeat_interval_seconds=self._pair_lease_hb,
        ) as session:
            if not session.acquired:
                # A crossing pair holds one character. Skip this occurrence and let
                # the chain advance to the next cadence — the crossing encounter
                # frees the character well within one interval. No re-run, no lock.
                contended = True
            elif gate is not None and not await self._pair_gate_allows(
                char_a, char_b, gate,
            ):
                # §4.3.2: both sides must be on-phase BEFORE the LLM. Release (on
                # exit) and re-schedule the pair forward — rewrite due, don't retry.
                gated = True
            else:
                try:
                    # Thread the lease's cooperative-abort hook into the encounter
                    # stages so a lease stolen mid-run (heartbeat renewal lost)
                    # stops the losing worker at the NEXT stage boundary — between
                    # plan and run, and before each encounter's run claim — instead
                    # of only being noticed after ``step_pair`` fully returns. A null
                    # (self-host lease-less) session never loses its lease, so this
                    # is a no-op there (byte-identical).
                    await self._encounters.step_pair(
                        relationship, now=resolved, gate=None,
                        abort=session.raise_if_lost,
                    )
                    executed = True
                except EncounterPairLeaseLost:
                    abandoned = True
                if session.lost:
                    # Lost mid-run: the reclaiming worker owns the pair (§ abandon).
                    abandoned = True
                    executed = False

        if abandoned:
            return SocialHandlerResult(kind, executed=False, abandoned=True)
        if gated:
            chained = await self._advance_chain(
                char_a, kind, resolved, relationship.id, last_due, fencing_epoch,
                co_characters=(char_b,),
                explicit_next_due=resolved + timedelta(seconds=_GATE_DEFER_SECONDS),
                # Sub-interval push (300s < 1800s base grid): key it in the defer
                # namespace so it does not collide with THIS still-claimed job's
                # base-grid key (which would drop the enqueue as a duplicate and
                # break the chain until the 15-min reconciler).
                defer_grid_seconds=_GATE_DEFER_SECONDS,
                extra_payload=self._pair_payload(char_a, char_b, relationship),
            )
            return SocialHandlerResult(
                kind, executed=False, deferred=True, next_enqueued=chained,
            )
        chained = await self._advance_chain(
            char_a, kind, resolved, relationship.id, last_due, fencing_epoch,
            co_characters=(char_b,),
            extra_payload=self._pair_payload(char_a, char_b, relationship),
        )
        return SocialHandlerResult(
            kind, executed=executed, next_enqueued=chained,
            lease_contended=contended,
        )

    async def _pair_gate_allows(
        self,
        char_a: Character,
        char_b: Character,
        gate: BackgroundActivityGatePort,
    ) -> bool:
        """Both-side AND gate for a pair encounter (§4.3.2 / embedded parity).

        A/b slots are lexicographic, and idle downshift is per-character, so a
        single-sided check would tie the pair's phase to id ordering. Fail-open on
        a gate error — a broken gate must not silently starve encounters."""
        try:
            return (
                await gate.allows(char_a, BackgroundActivityClass.ENCOUNTER_RUN)
                and await gate.allows(char_b, BackgroundActivityClass.ENCOUNTER_RUN)
            )
        except Exception:
            _LOGGER.exception(
                "encounter pair gate check failed pair=%s+%s",
                char_a.id, char_b.id,
            )
            return True

    @staticmethod
    def _pair_payload(
        char_a: Character, char_b: Character, relationship: CharacterRelationship,
    ) -> dict[str, str]:
        return {
            "character_a_id": char_a.id,
            "character_b_id": char_b.id,
            "relationship_id": relationship.id,
        }

    # -- shared self-chain advance ---------------------------------------- #

    async def _advance_chain(
        self,
        character: Character,
        kind: str,
        now: datetime,
        scope_id: str,
        last_due: datetime | None,
        fencing_epoch: int | None,
        *,
        co_characters: tuple[Character, ...] = (),
        explicit_next_due: datetime | None = None,
        defer_grid_seconds: float | None = None,
        extra_payload: dict[str, str] | None = None,
    ) -> bool:
        # Same cross-process fencing rule as CharacterKindHandler: the next chain
        # link is enqueued under the CLAIMED job's own epoch (a same-owner renewal
        # keeps it; a takeover bumps it and the enqueue fences out → reconciler
        # reseeds). ``epoch_provider`` is the combined-process fallback.
        epoch = fencing_epoch if fencing_epoch is not None else self._epoch_provider()
        if epoch is None:
            return False
        next_due = await self._calculator.compute(
            character, kind, now=now, last_due=last_due,
            explicit_next_due=explicit_next_due, scope_id=scope_id,
            defer_grid_seconds=defer_grid_seconds,
            # A pair chain belongs to both characters: ``character`` here is
            # only the canonical-low side, so the stop conditions must see the
            # other one too (NF4 dormancy is "any side stops it", like freeze).
            co_characters=co_characters,
        )
        if next_due is None:
            return False  # frozen / dormant mid-run — chain stops
        operator_id = character.user_id or None
        payload: dict[str, object] = {"chained": True}
        if extra_payload is not None:
            payload.update(extra_payload)
        else:
            payload["character_id"] = character.id
        spec = BackgroundJobSpec(
            kind=kind,
            idempotency_key=next_due.idempotency_key,
            due_at=next_due.due_at,
            fencing_epoch=epoch,
            priority=next_due.priority,
            tenant_id=await self._resolve_tenant(operator_id),
            operator_id=operator_id,
            # For a pair chain the character_id slot carries the canonical pair id
            # (relationship id) so active_chain_keys() diffs it as (kind, pair_id).
            character_id=scope_id,
            payload=payload,
        )
        try:
            job_id = await self._queue.enqueue(spec, now=now)
        except Exception:
            _LOGGER.exception(
                "social due-job chain: next enqueue failed kind=%s scope=%s",
                kind, scope_id,
            )
            return False
        return job_id is not None

    async def _resolve_tenant(self, operator_id: str | None) -> str | None:
        if self._operator_profiles is None or operator_id is None:
            return None
        try:
            operator = await self._operator_profiles.get(operator_id)
        except Exception:
            _LOGGER.exception(
                "social due-job chain: tenant resolve failed operator=%s",
                operator_id,
            )
            return None
        return operator.cloud_tenant_id if operator is not None else None

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
