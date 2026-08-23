"""Low-frequency chain reconciler for the social split (§13 / §4.2).

The character reconciler (:class:`DueJobReconciler`) relinks a character's missing
per-kind chains. This companion relinks the CROSS-character social chains the
``social_tick`` split introduced:

* ``persona_dream`` / ``peer_knowledge`` — per active character, exactly like the
  character reconciler (a new / thawed character with no queued social chain gets a
  now-due reseed).
* ``encounter_tick`` — per *enabled relationship* (canonical pair). This is the
  ``active_chain_keys`` extension the plan calls for: the pair chain's key is the
  relationship id (stored in the job's ``character_id`` slot), so the same
  ``(kind, key)`` diff detects a pair with no queued encounter chain — a new
  relationship, or a broken chain — and reseeds it. A cross-operator or frozen pair
  computes ``None`` and is left stopped (it re-links on re-enable / thaw).

It never runs providers, and is **distributed-only** (``run_once`` is a no-op
outside ``distributed`` mode, § red line / §15 barrier). Reseeds carry a stable
per-(scope, kind) jitter so a mass thaw does not stampede the worker. The fencing
epoch is the coordinator's currently-held leader epoch (shared, not a second lease).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.character_relationship_service import (
    canonical_pair,
)
from kokoro_link.application.services.due_job_reconciler import (
    EpochProvider,
    _DEFAULT_RESEED_JITTER_SECONDS,
    _stable_reseed_jitter,
)
from kokoro_link.application.services.account_runtime_profile_cache import (
    ProfileResolutionScope,
)
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.background_jobs import (
    BackgroundJobQueuePort,
    BackgroundJobSpec,
)
from kokoro_link.contracts.character_relationship import (
    CharacterRelationshipRepositoryPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import (
    ENCOUNTER_TICK_KIND,
    social_character_chain_kinds,
    social_pair_chain_kinds,
)
from kokoro_link.contracts.execution_mode import (
    MODE_DISTRIBUTED,
    RuntimeOwnershipPort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SocialReconcileResult:
    """What one social reconcile pass did (observability / test assertions)."""

    character_reseeded: int = 0
    pair_reseeded: int = 0
    skipped_chained: int = 0
    skipped_stopped: int = 0
    characters: int = 0
    relationships: int = 0


class SocialDueJobReconciler:
    """Reseeds missing social chains (per character + per pair; distributed-only)."""

    def __init__(
        self,
        *,
        queue: BackgroundJobQueuePort,
        character_repository: CharacterRepositoryPort,
        relationship_repository: CharacterRelationshipRepositoryPort,
        next_due_calculator: NextDueCalculator,
        epoch_provider: EpochProvider,
        runtime_ownership: RuntimeOwnershipPort | None = None,
        operator_profile_repository: OperatorProfileRepositoryPort | None = None,
        clock: ClockPort | None = None,
        reseed_jitter_seconds: int = _DEFAULT_RESEED_JITTER_SECONDS,
    ) -> None:
        self._queue = queue
        self._characters = character_repository
        self._relationships = relationship_repository
        self._calculator = next_due_calculator
        self._epoch_provider = epoch_provider
        self._runtime_ownership = runtime_ownership
        self._operator_profiles = operator_profile_repository
        self._clock = clock
        self._reseed_jitter_seconds = max(0, reseed_jitter_seconds)

    async def run_once(self, *, now: datetime | None = None) -> SocialReconcileResult:
        resolved = self._resolve_now(now)
        if self._runtime_ownership is not None:
            try:
                mode, _ = await self._runtime_ownership.get()
            except Exception:
                _LOGGER.exception("social reconciler: ownership read failed")
                return SocialReconcileResult()
            if mode != MODE_DISTRIBUTED:
                return SocialReconcileResult()
        epoch = self._epoch_provider()
        if epoch is None:
            return SocialReconcileResult()
        try:
            active_pairs = await self._queue.active_chain_keys()
            # Dead social chains (per character dream/peer + per pair encounter)
            # count as "present" so a poison chain is not auto-reseeded every pass
            # (dead = admin manual redrive only, 拍板 2026-07-20); redrive / 30d
            # prune lifts the block.
            active_pairs |= await self._queue.dead_chain_keys()
        except Exception:
            _LOGGER.exception("social reconciler: active-chain read failed")
            return SocialReconcileResult()

        tenant_cache: dict[str, str | None] = {}
        # One profile resolution per operator for the whole pass — characters
        # and pairs share it, since a pair's two sides are always the same
        # operator's. See :meth:`NextDueCalculator.new_profile_scope`.
        profile_scope = self._calculator.new_profile_scope()
        char_result = await self._reconcile_characters(
            resolved, epoch, active_pairs, tenant_cache, profile_scope,
        )
        pair_result = await self._reconcile_pairs(
            resolved, epoch, active_pairs, tenant_cache, profile_scope,
        )
        return SocialReconcileResult(
            character_reseeded=char_result[0],
            pair_reseeded=pair_result[0],
            skipped_chained=char_result[1] + pair_result[1],
            skipped_stopped=char_result[2] + pair_result[2],
            characters=char_result[3],
            relationships=pair_result[3],
        )

    async def _reconcile_characters(
        self,
        now: datetime,
        epoch: int,
        active_pairs: set[tuple[str, str]],
        tenant_cache: dict[str, str | None],
        profile_scope: ProfileResolutionScope,
    ) -> tuple[int, int, int, int]:
        try:
            characters = await self._characters.list_active()
        except Exception:
            _LOGGER.exception("social reconciler: character list failed")
            return (0, 0, 0, 0)
        reseeded = skipped_chained = skipped_stopped = 0
        kinds = social_character_chain_kinds()
        for character in characters:
            for kind in kinds:
                if (kind, character.id) in active_pairs:
                    skipped_chained += 1
                    continue
                seeded = await self._reseed(
                    character, kind, character.id, now, epoch, tenant_cache,
                    payload={"character_id": character.id, "reseeded": True},
                    profile_scope=profile_scope,
                )
                if seeded is True:
                    reseeded += 1
                elif seeded is None:
                    skipped_stopped += 1
                else:
                    skipped_chained += 1
        return (reseeded, skipped_chained, skipped_stopped, len(characters))

    async def _reconcile_pairs(
        self,
        now: datetime,
        epoch: int,
        active_pairs: set[tuple[str, str]],
        tenant_cache: dict[str, str | None],
        profile_scope: ProfileResolutionScope,
    ) -> tuple[int, int, int, int]:
        try:
            relationships = await self._relationships.list_enabled()
        except Exception:
            _LOGGER.exception("social reconciler: relationship list failed")
            return (0, 0, 0, 0)
        reseeded = skipped_chained = skipped_stopped = 0
        kinds = social_pair_chain_kinds()
        for relationship in relationships:
            char_a, char_b, stopped = await self._pair_characters(relationship)
            for kind in kinds:
                if (kind, relationship.id) in active_pairs:
                    skipped_chained += 1
                    continue
                if stopped or char_a is None:
                    # Missing / cross-operator / frozen pair — leave stopped.
                    skipped_stopped += 1
                    continue
                seeded = await self._reseed(
                    char_a, kind, relationship.id, now, epoch, tenant_cache,
                    payload={
                        "character_a_id": char_a.id,
                        "character_b_id": char_b.id if char_b else "",
                        "relationship_id": relationship.id,
                        "reseeded": True,
                    },
                    profile_scope=profile_scope,
                    # The pair's OTHER side. ``char_a`` is only the canonical
                    # -low id, not "the character this chain is for": asking
                    # the calculator about it alone would make the pair's stop
                    # conditions depend on id ordering — a chain kept alive by
                    # an active char_a for a char_b nobody has ever spoken to,
                    # and the mirror image stopping a pair whose active side
                    # happens to sort second. Every other stop here (missing /
                    # cross-operator / frozen / locked) is already "either
                    # side"; dormancy joins them.
                    co_characters=(char_b,) if char_b is not None else (),
                )
                if seeded is True:
                    reseeded += 1
                elif seeded is None:
                    skipped_stopped += 1
                else:
                    skipped_chained += 1
        return (reseeded, skipped_chained, skipped_stopped, len(relationships))

    async def _pair_characters(
        self, relationship,  # noqa: ANN001
    ) -> tuple[Character | None, Character | None, bool]:
        """Load a canonical pair; ``stopped`` when it must not be reseeded.

        A frozen / subscription-locked / cross-operator pair (or one whose
        characters are gone) is ``stopped`` so the pair chain stays broken until
        re-enable / thaw — mirroring the encounter planner's own skip rules."""
        try:
            low, high = canonical_pair(
                relationship.character_a_id, relationship.character_b_id,
            )
        except Exception:
            return (None, None, True)
        char_a = await self._characters.get(low)
        char_b = await self._characters.get(high)
        if char_a is None or char_b is None:
            return (char_a, char_b, True)
        if char_a.user_id != char_b.user_id:
            return (char_a, char_b, True)
        stopped = (
            char_a.frozen or char_b.frozen
            or char_a.subscription_locked or char_b.subscription_locked
        )
        return (char_a, char_b, stopped)

    async def _reseed(
        self,
        character: Character,
        kind: str,
        scope_id: str,
        now: datetime,
        epoch: int,
        tenant_cache: dict[str, str | None],
        *,
        payload: dict[str, object],
        profile_scope: ProfileResolutionScope | None = None,
        co_characters: tuple[Character, ...] = (),
    ) -> bool | None:
        """Reseed one chain. ``True`` seeded, ``False`` lost the race / covered,
        ``None`` intentionally stopped (calculator returned ``None``)."""
        reseed_due = now + timedelta(
            seconds=_stable_reseed_jitter(scope_id, kind, self._reseed_jitter_seconds),
        )
        next_due = await self._calculator.compute(
            character, kind, now=now, explicit_next_due=reseed_due, scope_id=scope_id,
            co_characters=co_characters, profile_scope=profile_scope,
        )
        if next_due is None:
            return None
        operator_id = character.user_id or None
        spec = BackgroundJobSpec(
            kind=kind,
            idempotency_key=next_due.idempotency_key,
            due_at=next_due.due_at,
            fencing_epoch=epoch,
            priority=next_due.priority,
            tenant_id=await self._resolve_tenant(operator_id, tenant_cache),
            operator_id=operator_id,
            character_id=scope_id,
            payload=payload,
        )
        try:
            job_id = await self._queue.enqueue(spec, now=now)
        except Exception:
            _LOGGER.exception(
                "social reconciler: reseed enqueue failed kind=%s scope=%s",
                kind, scope_id,
            )
            return False
        return job_id is not None

    async def _resolve_tenant(
        self, operator_id: str | None, cache: dict[str, str | None],
    ) -> str | None:
        if self._operator_profiles is None or operator_id is None:
            return None
        if operator_id in cache:
            return cache[operator_id]
        try:
            operator = await self._operator_profiles.get(operator_id)
        except Exception:
            _LOGGER.exception(
                "social reconciler: tenant resolve failed operator=%s", operator_id,
            )
            operator = None
        tenant = operator.cloud_tenant_id if operator is not None else None
        cache[operator_id] = tenant
        return tenant

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
