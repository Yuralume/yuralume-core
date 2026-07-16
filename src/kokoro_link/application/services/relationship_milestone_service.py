"""Relationship milestone observer.

When the per-(character, operator) interaction-volume band crosses a
threshold (e.g. stranger -> acquaintance), append a fixed-high salience
``relationship_milestone`` memory. The next chat turn picks it up through
``_render_relationship_milestones_block`` in
``infrastructure/prompt/default.py`` so the interaction change is felt in
the character's voice instead of being a hidden number.

Design notes (HUMANIZATION_ROADMAP §3.5):

- **LLM-first 紅線**: the trigger is *band crossing*, not "let's enumerate
  emotional milestones". The character's reaction to the milestone is
  left entirely to the LLM via the prompt block. We only write a fact
  about interaction volume, never prescribe behaviour or override an
  initial relationship seed.
- **No new schema**: the previous band is read back from the most-recent
  ``relationship_milestone`` row's ``tags`` (``band:<value>``). This
  keeps the entity / migration count flat — the prompt block + tag
  convention is all the persistence we need.
- **Feature flag**: ``humanization.relationship_milestone_enabled``
  defaults on; flip via ``KOKORO_HUMANIZATION_RELATIONSHIP_MILESTONE_ENABLED``.
- **Idempotent**: running twice between crossings is a no-op — the
  service compares current band against the latest stored band.
- **Offline only**: invoked from the dream pass tail. The hot chat path
  never calls this; we do not want any inline LLM-equivalent cost.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.domain.entities.memory_item import (
    MEMORY_AUDIENCE_PRIVATE,
    MemoryItem,
)
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_OPERATOR_LANGUAGE = "zh-TW"

_BAND_LABEL_KEYS: dict[str, str] = {
    "stranger": "milestone.band.stranger",
    "acquaintance": "milestone.band.acquaintance",
    "familiar": "milestone.band.familiar",
    "close": "milestone.band.close",
}


class RelationshipMilestoneService:
    """Observe Familiarity-band crossings; append milestone memories."""

    def __init__(
        self,
        *,
        persona_service,
        memory_repository: MemoryRepositoryPort,
        settings: HumanizationSettings,
        operator_profile_service: Any | None = None,
    ) -> None:
        self._persona_service = persona_service
        self._memory_repository = memory_repository
        self._settings = settings
        self._operator_profile_service = operator_profile_service

    async def check_and_emit(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime | None = None,
    ) -> MemoryItem | None:
        """Compare current band against the most recent stored milestone;
        write a new milestone memory iff the band changed.

        Returns the stored ``MemoryItem`` when one was created, else
        ``None`` (feature off, no interaction yet, or no crossing).
        """
        if not self._settings.relationship_milestone_enabled:
            return None

        try:
            strength = await self._persona_service.get_interaction_strength(
                character_id, operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "interaction strength lookup failed (char=%s op=%s)",
                character_id, operator_id,
            )
            return None

        if strength is None or strength.first_message_at is None:
            return None
        if strength.total_user_messages <= 0:
            return None

        current_band_value = strength.familiarity_band.value
        prev_band_value = await self._latest_band(character_id)

        # First milestone — skip if the relationship is still at "stranger"
        # so brand-new pairs do not spam the memory pool with a no-op
        # anchor. The regular relationship_anchor_block already handles
        # the no-memories case.
        if prev_band_value is None:
            if current_band_value == "stranger":
                return None
        else:
            if prev_band_value == current_band_value:
                return None

        language = await self._resolve_operator_language(operator_id)
        content = self._compose_content(
            prev_band_value, current_band_value, language,
        )
        memory = MemoryItem.create(
            character_id=character_id,
            kind=MemoryKind.RELATIONSHIP_MILESTONE,
            content=content,
            salience=1.0,
            tags=(
                "relationship_milestone",
                f"band:{current_band_value}",
                f"operator:{operator_id}",
            ),
            created_at=now or datetime.now(timezone.utc),
            # Per-operator trust/interaction book-keeping — recall-worthy
            # but never a public post (also gated by kind at the feed
            # collector; marked here so the row is honest at rest).
            audience=MEMORY_AUDIENCE_PRIVATE,
        )

        try:
            return await self._memory_repository.add(memory)
        except Exception:
            _LOGGER.exception(
                "relationship_milestone add failed (char=%s op=%s %s→%s)",
                character_id,
                operator_id,
                prev_band_value,
                current_band_value,
            )
            return None

    async def _latest_band(self, character_id: str) -> str | None:
        """Read the most recent stored band from the milestone tag.

        Falls back to ``None`` when no milestone exists (first run) or
        the tag is malformed. ``world_scope=None`` matches chat-path
        memory filtering, avoiding stale world-scoped rows.
        """
        try:
            existing = await self._memory_repository.query(
                character_id,
                kinds=[MemoryKind.RELATIONSHIP_MILESTONE],
                limit=1,
                world_scope=None,
            )
        except Exception:
            _LOGGER.exception(
                "relationship_milestone query failed (char=%s)", character_id,
            )
            return None
        if not existing:
            return None
        latest = existing[0]
        for tag in latest.tags:
            if tag.startswith("band:"):
                return tag.split(":", 1)[1].strip() or None
        return None

    async def _resolve_operator_language(self, operator_id: str) -> str:
        """Resolve the operator's content language for the milestone
        narration. Falls back to the ship-first ``zh-TW`` when no
        profile service is wired or resolution fails (legacy / tests),
        mirroring ``schedule_memorializer._resolve_operator_language``."""
        if self._operator_profile_service is None:
            return _DEFAULT_OPERATOR_LANGUAGE
        try:
            operator = await self._operator_profile_service.get_for_user(
                operator_id,
            )
        except Exception:  # pragma: no cover - defensive
            return _DEFAULT_OPERATOR_LANGUAGE
        if operator is None:
            return _DEFAULT_OPERATOR_LANGUAGE
        lang = (getattr(operator, "primary_language", "") or "").strip()
        return lang or _DEFAULT_OPERATOR_LANGUAGE

    @staticmethod
    def _band_label(band: str, language: str) -> str:
        key = _BAND_LABEL_KEYS.get(band)
        if key is None:
            return band
        return localized_fallback_text(key, language)

    @classmethod
    def _compose_content(
        cls, prev: str | None, current: str, language: str = _DEFAULT_OPERATOR_LANGUAGE,
    ) -> str:
        current_label = cls._band_label(current, language)
        if prev is None:
            return localized_fallback_text(
                "milestone.first_crossing", language,
                current_label=current_label,
            )
        prev_label = cls._band_label(prev, language)
        return localized_fallback_text(
            "milestone.band_upgrade", language,
            prev_label=prev_label, current_label=current_label,
        )
