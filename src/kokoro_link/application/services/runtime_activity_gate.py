from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.background_activity_gate import (
    BackgroundActivityClass,
    BackgroundActivityGatePort,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_IDLE_ANCHOR_CACHE_TTL = timedelta(minutes=5)
_GLOBAL_ACTIVITY_CLASSES = frozenset({
    BackgroundActivityClass.ENCOUNTER_RUN,
    BackgroundActivityClass.ENCOUNTER_PLAN,
    BackgroundActivityClass.PEER_KNOWLEDGE,
    BackgroundActivityClass.PERSONA_DREAM,
})


@dataclass(frozen=True, slots=True)
class _CharacterPolicy:
    profile: AccountRuntimeProfile
    idle: bool


@dataclass(frozen=True, slots=True)
class _CachedIdleAnchor:
    expires_at: datetime
    anchor: datetime | None


class RuntimeActivityTickGate(BackgroundActivityGatePort):
    def __init__(
        self,
        *,
        policies: dict[str, _CharacterPolicy],
        characters_by_id: dict[str, Character],
        proactive_phases: dict[str, int],
        feed_phases: dict[str, int],
        operator_phases: dict[tuple[str, BackgroundActivityClass], int],
    ) -> None:
        self._policies = policies
        self._characters_by_id = characters_by_id
        self._proactive_phases = proactive_phases
        self._feed_phases = feed_phases
        self._operator_phases = operator_phases

    async def allows_proactive(self, character: Character) -> bool:
        policy = self._policies.get(character.id)
        if policy is None:
            return False
        return _phase_allows(
            self._proactive_phases.get(character.id),
            policy.profile.effective_proactive_multiplier(idle=policy.idle),
        )

    async def allows(
        self,
        character: Character,
        activity_class: BackgroundActivityClass,
    ) -> bool:
        policy = self._policies.get(character.id)
        if policy is None:
            return False
        multiplier = policy.profile.effective_background_multiplier(
            idle=policy.idle,
        )
        if activity_class is BackgroundActivityClass.FEED_COMPOSE:
            phase = self._feed_phases.get(character.id)
        elif activity_class in _GLOBAL_ACTIVITY_CLASSES:
            phase = self._operator_phases.get((character.user_id, activity_class))
        else:
            return False
        return _phase_allows(phase, multiplier)

    def any_operator_allows(
        self,
        activity_class: BackgroundActivityClass,
    ) -> bool:
        if activity_class not in _GLOBAL_ACTIVITY_CLASSES:
            return False
        for character_id, policy in self._policies.items():
            character = self._characters_by_id[character_id]
            phase = self._operator_phases.get((character.user_id, activity_class))
            multiplier = policy.profile.effective_background_multiplier(
                idle=policy.idle,
            )
            if _phase_allows(phase, multiplier):
                return True
        return False


class DistributedGateView(BackgroundActivityGatePort):
    """Bucket-phased cost gate for the distributed worker (P3-C §5).

    Satisfies :class:`CharacterGateView` (``allows`` / ``allows_proactive``) AND
    the full :class:`BackgroundActivityGatePort` (``any_operator_allows``) that
    the social encounter/peer/dream steps need.

    Rate parity with the embedded :class:`RuntimeActivityTickGate` is the
    invariant: both honour the B1 policy multiplier as ``x % multiplier == 0``.
    The ONLY difference is the phase *basis* — the embedded gate uses per-entity
    tick counters that live in the singleton scheduler's memory (unavailable to a
    stateless, horizontally-scaled worker); this view uses the job's BUCKET
    number, which every worker computes deterministically from the same clock.
    Over consecutive buckets the allowed RATE is identical (1/multiplier); only
    the phase ALIGNMENT (which specific buckets fire) differs from the embedded
    counter phase — an accepted, documented divergence (§5)."""

    def __init__(
        self,
        *,
        policies: dict[str, _CharacterPolicy],
        characters_by_id: dict[str, Character],
        bucket: int,
    ) -> None:
        self._policies = policies
        self._characters_by_id = characters_by_id
        self._bucket = bucket
        # Global cadence is operator-scoped in embedded mode: each operator's
        # independent counter can be on-phase even when another is off-phase.
        # Preserve that OR semantics deterministically by carrying a stable
        # phase per (operator, activity class) into the distributed snapshot.
        self._operator_phases = {
            (character.user_id, activity_class): _stable_operator_phase(
                character.user_id, activity_class,
            )
            for character in characters_by_id.values()
            for activity_class in _GLOBAL_ACTIVITY_CLASSES
        }

    async def allows_proactive(self, character: Character) -> bool:
        policy = self._policies.get(character.id)
        if policy is None:
            return False
        return _phase_allows(
            self._bucket,
            policy.profile.effective_proactive_multiplier(idle=policy.idle),
        )

    async def allows(
        self,
        character: Character,
        activity_class: BackgroundActivityClass,
    ) -> bool:
        policy = self._policies.get(character.id)
        if policy is None:
            return False
        if (
            activity_class is not BackgroundActivityClass.FEED_COMPOSE
            and activity_class not in _GLOBAL_ACTIVITY_CLASSES
        ):
            return False
        multiplier = policy.profile.effective_background_multiplier(
            idle=policy.idle,
        )
        return _phase_allows(self._bucket, multiplier)

    def any_operator_allows(
        self,
        activity_class: BackgroundActivityClass,
    ) -> bool:
        if activity_class not in _GLOBAL_ACTIVITY_CLASSES:
            return False
        for character_id, policy in self._policies.items():
            character = self._characters_by_id[character_id]
            multiplier = policy.profile.effective_background_multiplier(
                idle=policy.idle,
            )
            phase = self._operator_phases[(character.user_id, activity_class)]
            if _phase_allows(self._bucket + phase, multiplier):
                return True
        return False


class RuntimeActivityGateService:
    """Build one immutable cost-gate snapshot for each scheduler tick."""

    def __init__(
        self,
        *,
        resolver: AccountRuntimeProfileResolverPort,
        character_repository: CharacterRepositoryPort,
        idle_anchor_cache_ttl: timedelta = _DEFAULT_IDLE_ANCHOR_CACHE_TTL,
    ) -> None:
        self._resolver = resolver
        self._characters = character_repository
        self._idle_anchor_cache_ttl = idle_anchor_cache_ttl
        self._idle_anchor_cache: dict[str, _CachedIdleAnchor] = {}
        self._proactive_counts: dict[str, int] = {}
        self._feed_counts: dict[str, int] = {}
        self._operator_counts: dict[
            tuple[str, BackgroundActivityClass], int
        ] = {}
        self._warned_freeze_orderings: set[tuple[str, int, int]] = set()

    async def prepare_tick(
        self,
        characters: list[Character],
        *,
        now: datetime,
        freeze_idle_days_threshold: int | None = None,
    ) -> RuntimeActivityTickGate:
        resolved_now = ensure_utc(now)
        profiles = await self._resolve_profiles(characters)
        policies: dict[str, _CharacterPolicy] = {}
        characters_by_id = {character.id: character for character in characters}
        operator_ids: set[str] = set()

        for character in characters:
            profile = profiles.get(character.user_id)
            if profile is None:
                continue
            operator_ids.add(character.user_id)
            self._warn_if_idle_after_freeze(
                profile,
                freeze_idle_days_threshold=freeze_idle_days_threshold,
            )
            policies[character.id] = _CharacterPolicy(
                profile=profile,
                idle=await self._is_idle(
                    character,
                    profile=profile,
                    now=resolved_now,
                ),
            )
            self._proactive_counts[character.id] = (
                self._proactive_counts.get(character.id, 0) + 1
            )
            self._feed_counts[character.id] = (
                self._feed_counts.get(character.id, 0) + 1
            )

        for operator_id in operator_ids:
            for activity_class in _GLOBAL_ACTIVITY_CLASSES:
                key = (operator_id, activity_class)
                self._operator_counts[key] = self._operator_counts.get(key, 0) + 1

        return RuntimeActivityTickGate(
            policies=policies,
            characters_by_id=characters_by_id,
            proactive_phases=dict(self._proactive_counts),
            feed_phases=dict(self._feed_counts),
            operator_phases=dict(self._operator_counts),
        )

    async def prepare_distributed(
        self,
        characters: list[Character],
        *,
        now: datetime,
        bucket: int,
        freeze_idle_days_threshold: int | None = None,
    ) -> DistributedGateView:
        """Build a bucket-phased gate for the distributed worker (P3-C §5).

        Reuses the EXACT profile-resolution + idle computation of
        :meth:`prepare_tick` (:meth:`_resolve_profiles` / :meth:`_is_idle`) so
        the B1 knob semantics are never duplicated — the only difference is the
        phase basis (the job ``bucket`` instead of the per-entity tick counters,
        which a stateless worker cannot hold). Unlike :meth:`prepare_tick` this
        mutates NO counters: the bucket is the phase, so there is nothing to
        advance."""
        resolved_now = ensure_utc(now)
        profiles = await self._resolve_profiles(characters)
        policies: dict[str, _CharacterPolicy] = {}
        characters_by_id = {character.id: character for character in characters}
        for character in characters:
            profile = profiles.get(character.user_id)
            if profile is None:
                continue
            self._warn_if_idle_after_freeze(
                profile,
                freeze_idle_days_threshold=freeze_idle_days_threshold,
            )
            policies[character.id] = _CharacterPolicy(
                profile=profile,
                idle=await self._is_idle(
                    character, profile=profile, now=resolved_now,
                ),
            )
        return DistributedGateView(
            policies=policies,
            characters_by_id=characters_by_id,
            bucket=bucket,
        )

    async def _resolve_profiles(
        self,
        characters: list[Character],
    ) -> dict[str, AccountRuntimeProfile | None]:
        profiles: dict[str, AccountRuntimeProfile | None] = {}
        for operator_id in dict.fromkeys(character.user_id for character in characters):
            try:
                profiles[operator_id] = await self._resolver.resolve_for_operator(
                    operator_id,
                )
            except Exception:
                _LOGGER.exception(
                    "runtime activity gate: profile resolve failed operator=%s",
                    operator_id,
                )
                profiles[operator_id] = None
        return profiles

    async def _is_idle(
        self,
        character: Character,
        *,
        profile: AccountRuntimeProfile,
        now: datetime,
    ) -> bool:
        idle_days = profile.idle_downshift_days
        if idle_days is None:
            return False
        anchor = await self._idle_anchor(character, now=now)
        if anchor is None:
            return False
        return now - anchor >= timedelta(days=idle_days)

    async def _idle_anchor(
        self,
        character: Character,
        *,
        now: datetime,
    ) -> datetime | None:
        state = getattr(character, "state", None)
        if state is not None and hasattr(state, "last_active_at"):
            last_active = state.last_active_at
            anchor = last_active if last_active is not None else character.created_at
            return ensure_utc(anchor) if anchor is not None else None

        cached = self._idle_anchor_cache.get(character.id)
        if cached is not None and now < cached.expires_at:
            return cached.anchor
        try:
            loaded = await self._characters.get(character.id)
        except Exception:
            _LOGGER.exception(
                "runtime activity gate: idle anchor lookup failed character=%s",
                character.id,
            )
            return None
        anchor = None
        if loaded is not None:
            last_active = loaded.state.last_active_at
            anchor = last_active if last_active is not None else loaded.created_at
            if anchor is not None:
                anchor = ensure_utc(anchor)
        self._idle_anchor_cache[character.id] = _CachedIdleAnchor(
            expires_at=now + self._idle_anchor_cache_ttl,
            anchor=anchor,
        )
        return anchor

    def _warn_if_idle_after_freeze(
        self,
        profile: AccountRuntimeProfile,
        *,
        freeze_idle_days_threshold: int | None,
    ) -> None:
        idle_days = profile.idle_downshift_days
        if (
            idle_days is None
            or freeze_idle_days_threshold is None
            or idle_days < freeze_idle_days_threshold
        ):
            return
        key = (profile.name, idle_days, freeze_idle_days_threshold)
        if key in self._warned_freeze_orderings:
            return
        self._warned_freeze_orderings.add(key)
        _LOGGER.warning(
            "account runtime profile %r: idle_downshift_days=%d is not below "
            "freeze idle_days_threshold=%d; idle downshift may never be observed",
            profile.name,
            idle_days,
            freeze_idle_days_threshold,
        )


def _stable_operator_phase(
    operator_id: str, activity_class: BackgroundActivityClass,
) -> int:
    digest = hashlib.sha256(
        f"{operator_id}:{activity_class.value}".encode("utf-8"),
    ).digest()
    return int.from_bytes(digest[:8], "big")

def _phase_allows(phase: int | None, multiplier: int) -> bool:
    return phase is not None and phase % multiplier == 0
