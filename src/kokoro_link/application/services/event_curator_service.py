"""Per-character event curator.

For each character, build an interest vector once and rank the recent
event window against it. High-similarity events go into the per-character
inbox where dispensers can claim them later.

Design principles (from CLAUDE.md):

- LLM-first / embedding-first matching: there are no keyword rules
  hard-coding which interest matches which category. Categories are a
  coarse pre-filter (operator's choice via ``subscribed_categories``);
  embedding cosine handles the fine grain.
- Generalisation: novel interests / new sources / new event topics
  flow through the same path. No special-casing.
- Region is a *pre-filter*, not a score term: the event pool is global
  but a hosted deployment serves players worldwide, and no amount of
  embedding similarity makes a Taiwanese local-news item useful to a
  player living in Japan. Events whose source is region-bound to
  somewhere else are removed before ranking; global (untagged) sources
  stay visible to everyone.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from kokoro_link.contracts.character_event_inbox import (
    CharacterEventInboxRepositoryPort,
)
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.world_event import WorldEventRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_event_inbox import (
    CharacterEventInboxItem,
)
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.region_code import normalise_region

logger = logging.getLogger(__name__)


class OperatorWorldEventRelevancePort(Protocol):
    async def get_current(self, character_id: str, operator_id: str): ...

    def render_world_event_relevance(self, persona) -> list[str]: ...


class OperatorRegionLookupPort(Protocol):
    """Structural view of ``OperatorProfileService`` used for region lookup.

    The curator only needs the owning player's profile; declaring the
    single method keeps the application service free of the concrete
    class and lets tests pass a two-line stub."""

    async def get_for_user(self, user_id: str): ...


@dataclass(frozen=True, slots=True)
class _RelevanceProfile:
    character_vector: tuple[float, ...] | None
    operator_vector: tuple[float, ...] | None
    operator_weight: float

    @property
    def has_signal(self) -> bool:
        return self.character_vector is not None or self.operator_vector is not None


class EventCuratorService:
    def __init__(
        self,
        *,
        world_event_repository: WorldEventRepositoryPort,
        inbox_repository: CharacterEventInboxRepositoryPort,
        embedder: EmbedderPort,
        window_days: int = 7,
        candidate_pool_size: int = 300,
        per_character_inbox_cap: int = 30,
        match_threshold: float = 0.30,
        exclude_threshold: float = 0.55,
        max_new_per_pass: int = 8,
        operator_persona_service: OperatorWorldEventRelevancePort | None = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        operator_profile_service: OperatorRegionLookupPort | None = None,
        operator_id: str = DEFAULT_OPERATOR_ID,
    ) -> None:
        self._events = world_event_repository
        self._inbox = inbox_repository
        self._embedder = embedder
        self._window_days = window_days
        self._candidate_pool_size = candidate_pool_size
        self._inbox_cap = per_character_inbox_cap
        self._match_threshold = match_threshold
        self._exclude_threshold = exclude_threshold
        self._max_new_per_pass = max_new_per_pass
        self._operator_persona_service = operator_persona_service
        self._relationship_seed_repository = relationship_seed_repository
        self._operator_profile_service = operator_profile_service
        self._operator_id = operator_id

    async def curate(self, character: Character) -> int:
        """Refresh the inbox for one character. Returns count added."""
        if not character.world_awareness_enabled:
            return 0
        if not self._embedder.is_operational:
            # Without an embedder we can't rank — silently no-op rather
            # than write nonsense rows.
            return 0

        profile = await self._build_relevance_profile(character)
        if not profile.has_signal:
            return 0

        excluded_vecs = await self._build_excluded_vectors(character)

        since = datetime.now(timezone.utc) - timedelta(
            days=self._window_days,
        )
        candidates = await self._events.list_with_embeddings_in_window(
            since=since,
            categories=(
                list(character.subscribed_categories)
                if character.subscribed_categories else None
            ),
            limit=self._candidate_pool_size,
            # Region narrowing precedes the embedding ranking on purpose:
            # relevance is scored *within* the material this player could
            # plausibly care about, instead of scoring a pool dominated by
            # another region and hoping the cosine sorts it out.
            operator_region=await self._resolve_operator_region(character),
        )
        if not candidates:
            return 0

        scored = self._score_candidates(candidates, profile, excluded_vecs)
        scored.sort(key=lambda t: t[1], reverse=True)

        new_items: list[CharacterEventInboxItem] = []
        for event, score in scored:
            if len(new_items) >= self._max_new_per_pass:
                break
            if score < self._match_threshold:
                break
            already = await self._inbox.has_event(character.id, event.id)
            if already:
                continue
            new_items.append(
                CharacterEventInboxItem.create(
                    character_id=character.id,
                    world_event_id=event.id,
                    similarity=score,
                    created_at=datetime.now(timezone.utc),
                )
            )

        if new_items:
            inserted = await self._inbox.add_many(new_items)
            if inserted:
                await self._inbox.trim_oldest(
                    character.id, keep=self._inbox_cap,
                )
            return inserted
        return 0

    async def _resolve_operator_region(
        self, character: Character,
    ) -> str | None:
        """Region of the player who owns this character, or ``None``.

        ``None`` means *no region filter at all* — the pool stays exactly
        what it is today. That is the self-host path (a single operator
        who never filled in a country) and the fail-soft path (service
        unwired, lookup blew up): a world-events feature must never go
        quiet because a profile read failed.
        """
        service = self._operator_profile_service
        if service is None:
            return None
        user_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            operator = await service.get_for_user(user_id)
        except Exception:
            logger.warning(
                "operator region lookup failed; curating unfiltered",
                extra={"character_id": character.id},
                exc_info=True,
            )
            return None
        return normalise_region(getattr(operator, "country_code", None))

    async def _build_relevance_profile(
        self, character: Character,
    ) -> _RelevanceProfile:
        character_vec = await self._build_character_vector(character)
        operator_vec, operator_weight = await self._build_operator_vector(
            character,
        )
        return _RelevanceProfile(
            character_vector=character_vec,
            operator_vector=operator_vec,
            operator_weight=operator_weight,
        )

    async def _build_character_vector(
        self, character: Character,
    ) -> tuple[float, ...] | None:
        # Compose a character-side embedding from interests +
        # world_topics + summary. Summary enriches an explicit
        # interest/topic profile, but does not by itself make the
        # character eligible — otherwise a sparse "ordinary student"
        # summary would invite noisy matches.
        if not character.interests and not character.world_topics:
            return None
        parts: list[str] = []
        if character.interests:
            parts.append("興趣：" + "、".join(character.interests))
        if character.world_topics:
            parts.append("關注主題：" + "、".join(character.world_topics))
        if character.summary:
            parts.append("人物：" + character.summary[:200])
        text = "\n".join(parts)
        try:
            vec = await self._embedder.embed(text)
        except EmbedderError as exc:
            logger.warning(
                "character relevance embed failed",
                extra={"character_id": character.id, "error": repr(exc)},
            )
            return None
        return vec

    async def _build_operator_vector(
        self, character: Character,
    ) -> tuple[tuple[float, ...] | None, float]:
        service = self._operator_persona_service
        if service is None:
            return None, 0.0
        try:
            persona = await service.get_current(character.id, self._operator_id)
            lines = service.render_world_event_relevance(persona)
        except Exception:
            logger.exception(
                "operator world-event relevance render failed",
                extra={"character_id": character.id},
            )
            return None, 0.0
        usable = [line.strip() for line in lines if line and line.strip()]
        if not usable:
            return None, 0.0
        text = "\n".join([
            "使用者公開、低風險、可作為日常話題的背景：",
            *usable,
            "這些只代表可能與使用者相關，不代表角色自己是該領域專家。",
        ])
        try:
            vec = await self._embedder.embed(text)
        except EmbedderError as exc:
            logger.warning(
                "operator relevance embed failed",
                extra={"character_id": character.id, "error": repr(exc)},
            )
            return None, 0.0
        has_seed = await self._has_initial_relationship_seed(character.id)
        return vec, _operator_relevance_weight(
            persona,
            has_initial_relationship_seed=has_seed,
        )

    async def _has_initial_relationship_seed(self, character_id: str) -> bool:
        repository = self._relationship_seed_repository
        if repository is None:
            return False
        try:
            seed = await repository.get(character_id, self._operator_id)
        except Exception:
            logger.exception(
                "operator initial relationship lookup failed for event curation",
                extra={"character_id": character_id},
            )
            return False
        return bool(seed is not None and not seed.is_empty)

    async def _build_excluded_vectors(
        self, character: Character,
    ) -> list[tuple[float, ...]]:
        if not character.excluded_topics:
            return []
        try:
            vecs = await self._embedder.embed_many(
                list(character.excluded_topics)
            )
        except EmbedderError as exc:
            logger.warning(
                "excluded embed failed",
                extra={"character_id": character.id, "error": repr(exc)},
            )
            return []
        return [v for v in vecs if v is not None]

    def _score_candidates(
        self,
        candidates: list[WorldEvent],
        profile: _RelevanceProfile,
        excluded_vecs: list[tuple[float, ...]],
    ) -> list[tuple[WorldEvent, float]]:
        out: list[tuple[WorldEvent, float]] = []
        for event in candidates:
            if not event.embedding:
                continue
            event_vec = tuple(event.embedding)
            scores: list[float] = []
            if profile.character_vector is not None:
                scores.append(_cosine(profile.character_vector, event_vec))
            if profile.operator_vector is not None:
                scores.append(
                    _cosine(profile.operator_vector, event_vec)
                    * profile.operator_weight,
                )
            if not scores:
                continue
            sim = max(scores)
            if excluded_vecs:
                max_excl = max(
                    _cosine(ex, event_vec) for ex in excluded_vecs
                )
                if max_excl >= self._exclude_threshold:
                    continue
            out.append((event, sim))
        return out


def _operator_relevance_weight(
    persona,  # noqa: ANN001
    *,
    has_initial_relationship_seed: bool = False,
) -> float:
    strength = getattr(persona, "layer4_interaction", None)
    band = getattr(strength, "familiarity_band", None)
    value = getattr(band, "value", "stranger")
    base = {
        "stranger": 0.35,
        "acquaintance": 0.6,
        "familiar": 0.8,
        "close": 1.0,
    }.get(str(value), 0.35)
    if has_initial_relationship_seed:
        return max(base, 0.6)
    return base


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
