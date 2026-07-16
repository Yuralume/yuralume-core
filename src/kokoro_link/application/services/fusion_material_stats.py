"""Fusion material-richness stats service (Creator Studio C1-P1).

Deterministic bookkeeping over the *same* salience-ranked memory slice
the fusion brief pulls (:func:`select_brief_memories`), so the
character-picker richness badge never drifts from the material a fusion
story would actually have to work with.

Per CREATOR_STUDIO_VALUE_LINE_PLAN §2.1-5 this is the calculated-
statistics carve-out the LLM-first rule allows: it only counts the
memories the brief already selected and their total length — it never
enumerates, keyword-matches, or judges content. Thresholds are
operator-configurable (``fusion_material`` site-settings group). Nothing
here blocks creation; a ``sparse`` result only tells the UI to show a
soft, positive nudge to chat more before fusing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsService,
)
from kokoro_link.application.services.fusion_character_brief import (
    select_brief_memories,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.infrastructure.app_runtime_settings.schemas import (
    FusionMaterialRuntimeConfig,
)

_LOGGER = logging.getLogger(__name__)

_FUSION_MATERIAL_GROUP = "fusion_material"

MaterialTier = Literal["rich", "ok", "sparse"]


@dataclass(frozen=True, slots=True)
class CharacterMaterialStats:
    """Per-character fusion material richness.

    ``memory_count`` / ``total_chars`` are the count and joined length of
    the memories the fusion brief would select for this character;
    ``tier`` is those two numbers graded against the configured
    thresholds."""

    character_id: str
    memory_count: int
    total_chars: int
    tier: MaterialTier


class FusionMaterialStatsService:
    """Grades characters' fusion material against configurable thresholds.

    The memory selection is delegated to :func:`select_brief_memories`
    (shared with the brief builder) so counts can never diverge from the
    prompt the pipeline actually assembles."""

    def __init__(
        self,
        *,
        memory_repository: MemoryRepositoryPort | None,
        settings_service: AppRuntimeSettingsService,
    ) -> None:
        self._memory_repository = memory_repository
        self._settings_service = settings_service

    async def stats_for(
        self, character_ids: Sequence[str],
    ) -> list[CharacterMaterialStats]:
        """Richness stats for each id, preserving the input order.

        Reads the threshold config once, then grades each character. A
        memory-store hiccup degrades that character to ``sparse`` / 0
        rather than failing the batch (see :func:`select_brief_memories`).
        """
        if not character_ids:
            return []
        config = await self._load_config()
        return [
            await self._stats_for_one(cid, config) for cid in character_ids
        ]

    async def _stats_for_one(
        self, character_id: str, config: FusionMaterialRuntimeConfig,
    ) -> CharacterMaterialStats:
        chosen = await select_brief_memories(
            self._memory_repository, character_id,
        )
        memory_count = len(chosen)
        total_chars = sum(len(item.content) for item in chosen)
        return CharacterMaterialStats(
            character_id=character_id,
            memory_count=memory_count,
            total_chars=total_chars,
            tier=self._classify(memory_count, total_chars, config),
        )

    @staticmethod
    def _classify(
        count: int, chars: int, config: FusionMaterialRuntimeConfig,
    ) -> MaterialTier:
        # Both dimensions must clear a tier's floor: a character with many
        # one-line memories (high count, low chars) or one long dump (low
        # count, high chars) is not yet "rich" fusion material.
        if count >= config.rich_min_count and chars >= config.rich_min_chars:
            return "rich"
        if count >= config.ok_min_count and chars >= config.ok_min_chars:
            return "ok"
        return "sparse"

    async def _load_config(self) -> FusionMaterialRuntimeConfig:
        try:
            config = await self._settings_service.get(
                _FUSION_MATERIAL_GROUP,
                default=FusionMaterialRuntimeConfig(),
            )
        except Exception:
            _LOGGER.exception(
                "fusion material stats: config read failed; using defaults",
            )
            return FusionMaterialRuntimeConfig()
        if isinstance(config, FusionMaterialRuntimeConfig):
            return config
        # Defensive: a mis-registered group could hand back a different
        # schema. Fall back to the schema default thresholds.
        return FusionMaterialRuntimeConfig()


__all__ = [
    "CharacterMaterialStats",
    "FusionMaterialStatsService",
    "MaterialTier",
]
