"""Player-side memoir aggregation service.

Projects three already-existing data sources into a unified
:class:`MemoirView`:

* the latest week / month :class:`SelfReflection` snapshots → chapters
* high-salience :class:`MemoryItem` rows (incl. ``relationship_milestone``)
* high-intensity :class:`EmotionEvent` rows

and overlays per-(character, operator) pin state. The service is the
**only** place where this aggregation logic lives — both the API route
and any future server-rendered view should call ``build_view``.

LLM-first guard: chapter narratives come from
``SelfReflection.narrative``; nothing in this file may template-format
or keyword-pattern its way into "what counts as memoir-worthy".
Filtering uses structured fields only (salience, intensity, kind,
``cause_ref_kind``, ``created_at``). The optional localizer is a
read-side language projection of already-selected player-visible text;
it must not invent memoir content or change structure.

Privacy guard: HEARSAY memories and ``idle_drift`` emotion events are
hard-excluded module-level constants to prevent regressions where a
PR quietly loosens the rule (PRODUCT.md §脆弱資料保護 +
HUMANIZATION_ROADMAP §3.2).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Final

from kokoro_link.bootstrap.settings import MemoirSettings
from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.contracts.memoir import MemoirPinRepositoryPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.memoir_localizer import MemoirLocalizerPort
from kokoro_link.contracts.self_reflection import (
    SelfReflectionRepositoryPort,
)
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_IDLE_DRIFT,
    EmotionEvent,
)
from kokoro_link.domain.entities.memoir import (
    ENTRY_EMOTION,
    ENTRY_MEMORY,
    ENTRY_MILESTONE,
    MemoirChapter,
    MemoirEntry,
    MemoirView,
)
from kokoro_link.domain.entities.memoir_pin import MemoirPin
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.self_reflection import SelfReflection
from kokoro_link.domain.value_objects.memory_kind import MemoryKind

_LOGGER = logging.getLogger(__name__)

_DEFAULT_PRIMARY_LANGUAGE: Final = "zh-TW"

_EXCLUDED_MEMORY_KINDS: Final[frozenset[str]] = frozenset({
    MemoryKind.HEARSAY.value,
})
"""HEARSAY rows record what *other* people said about the user, not the
character's own memories of the player. Surfacing them in a "我們的回憶
錄" view would conflate sources and violate the
``feedback_cross_channel_persona`` rule (a memoir is not a gossip log).

Excluded as a module-level constant so it cannot be quietly loosened by
a feature PR — any change must explicitly touch this name.
"""

_EXCLUDED_EMOTION_CAUSES: Final[frozenset[str]] = frozenset({
    CAUSE_IDLE_DRIFT,
})
"""``idle_drift`` emotion events read as "I felt lonely because you
didn't message me". Even at high intensity, showing them to the player
in a memoir frames the character as emotionally guilt-tripping the user
— directly violates PRODUCT.md §脆弱資料保護 ("禁止情緒勒索"). Other
``cause_ref_kind`` values stay in.
"""


class MemoirPinLimitExceededError(Exception):
    """Raised when a player tries to pin more entries than
    :attr:`MemoirSettings.pin_max_per_pair`. The API layer maps this to
    HTTP 409."""

    def __init__(self, *, current: int, limit: int) -> None:
        super().__init__(
            f"pin limit exceeded: {current}/{limit}",
        )
        self.current = current
        self.limit = limit


class MemoirService:
    def __init__(
        self,
        *,
        memory_repository: MemoryRepositoryPort,
        self_reflection_repository: SelfReflectionRepositoryPort,
        emotion_event_repository: EmotionEventRepositoryPort,
        pin_repository: MemoirPinRepositoryPort,
        settings: MemoirSettings,
        localizer: MemoirLocalizerPort | None = None,
        operator_profile_service=None,  # noqa: ANN001 - optional primary_language resolver
    ) -> None:
        self._memory_repository = memory_repository
        self._self_reflection_repository = self_reflection_repository
        self._emotion_event_repository = emotion_event_repository
        self._pin_repository = pin_repository
        self._settings = settings
        self._localizer = localizer
        self._operator_profile_service = operator_profile_service

    async def build_view(
        self, character_id: str, operator_id: str,
    ) -> MemoirView:
        """Assemble the read-only memoir view for one
        ``(character_id, operator_id)`` pair."""
        reflections = await self._self_reflection_repository.latest_for(
            character_id, operator_id,
        )
        memories = await self._memory_repository.list_all_for_character(
            character_id, world_scope=None,
        )
        since = datetime.now(timezone.utc) - timedelta(
            days=self._settings.emotion_lookback_days,
        )
        emotions = await self._emotion_event_repository.list_recent(
            character_id=character_id,
            operator_id=operator_id,
            since=since,
            limit=500,
        )
        pins = await self._pin_repository.list_for(character_id, operator_id)
        pin_index = self._index_pins(pins)

        chapters = self._build_chapters(reflections)
        timeline = self._build_timeline(
            memories=memories,
            emotions=emotions,
            pin_index=pin_index,
        )

        view = MemoirView(
            chapters=tuple(chapters),
            timeline=tuple(timeline),
            pin_count=len(pins),
            pin_limit=self._settings.pin_max_per_pair,
        )
        return await self._localize_for_operator(view, operator_id)

    async def pin(
        self,
        *,
        character_id: str,
        operator_id: str,
        entry_kind: str,
        entry_id: str,
    ) -> MemoirPin:
        """Pin a memoir entry for the given pair. Idempotent — re-pinning
        returns the existing pin.

        Raises :class:`MemoirPinLimitExceededError` when the pair has
        already pinned :attr:`MemoirSettings.pin_max_per_pair` distinct
        entries. The pre-check is racy under concurrent pins, which is
        acceptable: a few extra rows over the limit is a UX nit, not a
        correctness issue.
        """
        # Idempotent re-pin: if a pin already exists, return it without
        # tripping the limit (the row already counts toward the cap).
        existing = await self._find_pin(
            character_id, operator_id, entry_kind, entry_id,
        )
        if existing is not None:
            return existing
        current = await self._pin_repository.count_for(
            character_id, operator_id,
        )
        if current >= self._settings.pin_max_per_pair:
            raise MemoirPinLimitExceededError(
                current=current,
                limit=self._settings.pin_max_per_pair,
            )
        pin = MemoirPin.new(
            character_id=character_id,
            operator_id=operator_id,
            entry_kind=entry_kind,
            entry_id=entry_id,
        )
        return await self._pin_repository.add(pin)

    async def unpin(
        self,
        *,
        character_id: str,
        operator_id: str,
        entry_kind: str,
        entry_id: str,
    ) -> bool:
        """Remove a pin. Returns ``False`` when nothing was pinned
        (so the API layer can map to 404)."""
        return await self._pin_repository.remove(
            character_id, operator_id, entry_kind, entry_id,
        )

    # ------------------------------------------------------------------
    # internals — pure structural projection, no LLM, no keyword matching
    # ------------------------------------------------------------------

    async def _localize_for_operator(
        self,
        view: MemoirView,
        operator_id: str,
    ) -> MemoirView:
        localizer = self._localizer
        if localizer is None:
            return view
        target = await self._resolve_operator_language(operator_id)
        if not target or target == _DEFAULT_PRIMARY_LANGUAGE:
            return view
        try:
            return await localizer.localize_view(
                view,
                target_language=target,
            )
        except Exception:
            _LOGGER.exception(
                "memoir: localizer crashed operator=%s language=%s",
                operator_id,
                target,
            )
            return view

    async def _resolve_operator_language(self, operator_id: str) -> str:
        service = self._operator_profile_service
        if service is None:
            return _DEFAULT_PRIMARY_LANGUAGE
        try:
            operator = await service.get_for_user(operator_id)
        except Exception:
            _LOGGER.exception(
                "memoir: operator profile lookup failed operator=%s",
                operator_id,
            )
            return _DEFAULT_PRIMARY_LANGUAGE
        if operator is None:
            return _DEFAULT_PRIMARY_LANGUAGE
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or _DEFAULT_PRIMARY_LANGUAGE

    async def _find_pin(
        self,
        character_id: str,
        operator_id: str,
        entry_kind: str,
        entry_id: str,
    ) -> MemoirPin | None:
        pins = await self._pin_repository.list_for(character_id, operator_id)
        for pin in pins:
            if pin.entry_kind == entry_kind and pin.entry_id == entry_id:
                return pin
        return None

    @staticmethod
    def _index_pins(
        pins: list[MemoirPin],
    ) -> dict[tuple[str, str], MemoirPin]:
        return {(pin.entry_kind, pin.entry_id): pin for pin in pins}

    @staticmethod
    def _build_chapters(
        reflections: list[SelfReflection],
    ) -> list[MemoirChapter]:
        # Repository already returns at most one row per period (current
        # snapshot model); we project verbatim, preserving order: newest
        # first means week first if both exist on the same day, otherwise
        # whichever was written more recently.
        return [
            MemoirChapter(
                period=r.period,
                period_start=r.period_start,
                period_end=r.period_end,
                narrative=r.narrative,
                dominant_themes=r.dominant_themes,
                evidence_quotes=r.evidence_quotes,
            )
            for r in reflections
        ]

    def _build_timeline(
        self,
        *,
        memories: list[MemoryItem],
        emotions: list[EmotionEvent],
        pin_index: dict[tuple[str, str], MemoirPin],
    ) -> list[MemoirEntry]:
        entries: list[MemoirEntry] = []
        for item in self._filter_memories(memories):
            entries.append(self._memory_to_entry(item, pin_index))
        for event in self._filter_emotions(emotions):
            entries.append(self._emotion_to_entry(event, pin_index))
        entries.sort(
            key=lambda e: (not e.pinned, -e.occurred_at.timestamp()),
        )
        return entries[: self._settings.timeline_limit]

    def _filter_memories(
        self, items: list[MemoryItem],
    ) -> list[MemoryItem]:
        threshold = self._settings.memory_min_salience
        return [
            item for item in items
            if item.salience >= threshold
            and item.kind.value not in _EXCLUDED_MEMORY_KINDS
        ]

    def _filter_emotions(
        self, events: list[EmotionEvent],
    ) -> list[EmotionEvent]:
        threshold = self._settings.emotion_min_intensity
        return [
            event for event in events
            if event.intensity >= threshold
            and event.cause_ref_kind not in _EXCLUDED_EMOTION_CAUSES
        ]

    @staticmethod
    def _memory_to_entry(
        item: MemoryItem,
        pin_index: dict[tuple[str, str], MemoirPin],
    ) -> MemoirEntry:
        is_milestone = item.kind == MemoryKind.RELATIONSHIP_MILESTONE
        kind = ENTRY_MILESTONE if is_milestone else ENTRY_MEMORY
        extras: dict[str, str] = {"memory_kind": item.kind.value}
        if item.tags:
            extras["tags"] = ",".join(item.tags)
        return MemoirEntry(
            kind=kind,
            entry_id=item.id,
            occurred_at=item.created_at,
            summary=item.content,
            score=item.salience,
            pinned=(kind, item.id) in pin_index,
            extras=extras,
        )

    @staticmethod
    def _emotion_to_entry(
        event: EmotionEvent,
        pin_index: dict[tuple[str, str], MemoirPin],
    ) -> MemoirEntry:
        extras: dict[str, str] = {
            "cause_ref_kind": event.cause_ref_kind,
            "valence": f"{event.valence:.2f}",
            "arousal": f"{event.arousal:.2f}",
        }
        # Use emotion_label when present, otherwise fall back to evidence
        # quote so the timeline always has *something* to show.
        summary = event.emotion_label.strip() or event.evidence_quote.strip()
        if not summary:
            # Defensive: should never happen because aggregator/extractor
            # require at least a label, but we don't want MemoirEntry's
            # post-init to crash the whole view if a malformed row sneaks
            # in. Use the cause as a placeholder summary.
            summary = f"({event.cause_ref_kind})"
        if event.emotion_label:
            extras["emotion_label"] = event.emotion_label
        return MemoirEntry(
            kind=ENTRY_EMOTION,
            entry_id=event.id,
            occurred_at=event.created_at,
            summary=summary,
            score=event.intensity,
            pinned=(ENTRY_EMOTION, event.id) in pin_index,
            extras=extras,
        )
