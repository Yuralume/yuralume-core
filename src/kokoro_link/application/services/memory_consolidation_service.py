"""Orchestrates memory decay + LLM consolidation.

Two-stage pipeline:

1. **Decay**: drop memories that match the heuristic decay rule (low
   salience + old + never accessed). Pure bookkeeping, no LLM.
2. **Consolidation**: cluster surviving memories by embedding
   similarity, ask the LLM consolidator to merge each cluster into one
   higher-quality memory, swap the originals out.

Both stages support ``dry_run=True`` — nothing is mutated, the report
still describes what would have happened. Useful from the CLI to
preview before pulling the trigger on a large character pool.

Consolidated memories inherit:

- ``content`` / ``salience`` / ``tags``: from the LLM proposal
- ``kind``: same as the cluster (clustering never crosses kinds)
- ``character_id`` / ``conversation_id``: from the cluster's first item
- ``created_at``: the **oldest** original — preserves the temporal
  anchor so recency scoring still places the merged memory where the
  original knowledge began
- ``access_count``: sum of the originals (preserves "this was useful")
- ``embedding``: re-computed via the injected embedder at persist time
  so the merged vector actually represents the merged text
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.memory_consolidator import MemoryConsolidatorPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import (
    MEMORY_AUDIENCE_PRIVATE,
    MemoryItem,
)
from kokoro_link.infrastructure.memory.clustering import (
    cluster_by_similarity,
)
from kokoro_link.infrastructure.memory.decay import (
    DecayPolicy,
    plan_decay,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_SIMILARITY_THRESHOLD = 0.82
_DEFAULT_MIN_CLUSTER_SIZE = 2


@dataclass(frozen=True, slots=True)
class ConsolidationReport:
    character_id: str
    dry_run: bool
    decayed: int
    clusters_found: int
    clusters_merged: int
    memories_replaced: int
    memories_after: int


class MemoryConsolidationService:
    def __init__(
        self,
        *,
        memory_repository: MemoryRepositoryPort,
        consolidator: MemoryConsolidatorPort,
        embedder: EmbedderPort | None = None,
        character_repository: CharacterRepositoryPort | None = None,
        operator_profile_service=None,  # noqa: ANN001 - optional language resolver
    ) -> None:
        self._memory_repository = memory_repository
        self._consolidator = consolidator
        self._embedder = embedder
        self._character_repository = character_repository
        """Optional — used to look the character entity up once per
        consolidation run so the per-character LLM override (if any)
        applies to cluster merges. ``None`` (legacy / tests) falls back
        to global preferences, which matches the pre-feature behaviour."""
        self._operator_profile_service = operator_profile_service
        """Optional — resolves the operator's content language so merged
        memories (shown in MemoryBrowserPanel) follow the operator
        language instead of the consolidator's Chinese scaffolding.
        ``None`` (legacy / tests) falls back to the ship-first zh-TW."""

    async def consolidate(
        self,
        character_id: str,
        *,
        dry_run: bool = False,
        decay_only: bool = False,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
        min_cluster_size: int = _DEFAULT_MIN_CLUSTER_SIZE,
        decay_policy: DecayPolicy | None = None,
    ) -> ConsolidationReport:
        character: Character | None = None
        if self._character_repository is not None:
            character = await self._character_repository.get(character_id)
        operator_language = await self._resolve_operator_language(character)
        # --- Stage 1 — decay --------------------------------------
        all_memories = await self._memory_repository.list_all_for_character(
            character_id
        )
        plan = plan_decay(
            all_memories, character_id=character_id, policy=decay_policy,
        )

        decayed = 0
        survivors: list[MemoryItem] = all_memories
        if plan.item_ids:
            if dry_run:
                decayed = plan.count
                decayed_ids = set(plan.item_ids)
                survivors = [it for it in all_memories if it.id not in decayed_ids]
            else:
                decayed = await self._memory_repository.delete_many(plan.item_ids)
                survivors = [it for it in all_memories if it.id not in set(plan.item_ids)]

        # --- Stage 2 — consolidation ------------------------------
        clusters_found = 0
        clusters_merged = 0
        replaced = 0
        if not decay_only:
            clusters = cluster_by_similarity(
                survivors,
                similarity_threshold=similarity_threshold,
                min_cluster_size=min_cluster_size,
            )
            clusters_found = len(clusters)

            for cluster in clusters:
                if dry_run:
                    # We still count what we *would* merge so the user
                    # can preview.
                    clusters_merged += 1
                    replaced += len(cluster)
                    continue
                merged = await self._consolidate_cluster(
                    cluster,
                    character=character,
                    operator_primary_language=operator_language,
                )
                if merged is None:
                    continue
                clusters_merged += 1
                replaced += len(cluster)

        remaining = await self._memory_repository.count_for_character(character_id)
        return ConsolidationReport(
            character_id=character_id,
            dry_run=dry_run,
            decayed=decayed,
            clusters_found=clusters_found,
            clusters_merged=clusters_merged,
            memories_replaced=replaced,
            memories_after=remaining,
        )

    async def _resolve_operator_language(
        self, character: Character | None,
    ) -> str:
        """Resolve the operator's content language for merged-memory text.
        Falls back to the ship-first ``zh-TW`` when no profile service is
        wired or resolution fails (legacy / tests)."""
        default = "zh-TW"
        if character is None or self._operator_profile_service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await self._operator_profile_service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = (getattr(operator, "primary_language", "") or "").strip()
        return lang or default

    async def _consolidate_cluster(
        self,
        cluster: list[MemoryItem],
        *,
        character: Character | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> MemoryItem | None:
        proposal = await self._consolidator.merge(
            cluster,
            character=character,
            operator_primary_language=operator_primary_language,
        )
        if proposal is None:
            return None

        # Build the replacement with sensible metadata inheritance.
        oldest = min(cluster, key=lambda m: _as_utc(m.created_at))
        summed_access = sum(item.access_count for item in cluster)
        tags = tuple(proposal.tags) if proposal.tags else _merge_tags(cluster)
        # Privacy is monotone: if ANY merged member was private, the merged
        # text may restate that private fact, so the merge stays private
        # (fail-closed). Without this the merged row would default to ''
        # (feed-eligible), silently de-privatising a private memory.
        merged_audience = (
            MEMORY_AUDIENCE_PRIVATE
            if any(m.audience == MEMORY_AUDIENCE_PRIVATE for m in cluster)
            else ""
        )

        replacement = MemoryItem.create(
            character_id=oldest.character_id,
            kind=proposal.kind,
            content=proposal.content,
            salience=proposal.salience,
            conversation_id=oldest.conversation_id,
            tags=tags,
            created_at=_as_utc(oldest.created_at),
            audience=merged_audience,
        )
        # ``access_count`` isn't a create-kwarg — patch it via dataclass
        # replace so the merged item keeps its usage history.
        from dataclasses import replace

        replacement = replace(replacement, access_count=summed_access)

        # Fail-loud: the merged text deserves a fresh embedding. If the
        # embedder is down we **do not** write an embedding-less
        # replacement — we leave the cluster alone until the embedder
        # comes back.
        try:
            embedded = await attach_embeddings([replacement], self._embedder)
        except EmbedderError:
            _LOGGER.exception(
                "Embedder unavailable; skipping merge for cluster of %d items",
                len(cluster),
            )
            return None

        # Atomic-ish swap: add first, then delete originals. On repo
        # error between the two we'd have a temporary duplicate — the
        # dedup filter catches that on the next post-turn pass.
        await self._memory_repository.add_many(embedded)
        await self._memory_repository.delete_many([item.id for item in cluster])
        return embedded[0]


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _merge_tags(cluster: list[MemoryItem]) -> tuple[str, ...]:
    seen: list[str] = []
    for item in cluster:
        for tag in item.tags:
            if tag not in seen:
                seen.append(tag)
    return tuple(seen[:8])
