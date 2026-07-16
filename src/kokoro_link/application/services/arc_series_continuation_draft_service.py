"""Application service for concluded ArcSeries continuation drafts."""

from __future__ import annotations

from kokoro_link.application.services.arc_series_service import (
    ArcSeriesNotFoundError,
    ArcSeriesValidationError,
)
from kokoro_link.application.services.arc_template_intake_service import TemplateDraft
from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.contracts.arc_series_continuation import (
    ArcSeriesContinuationContext,
    ArcSeriesContinuationDraftPort,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.story import StoryEventRepositoryPort
from kokoro_link.contracts.story_arc import StoryArcRepositoryPort
from kokoro_link.domain.entities.arc_series import SERIES_STATUS_CONCLUDED
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.story_arc import ARC_COMPLETED, BEAT_REALIZED, StoryArc
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.memory_kind import MemoryKind


class ArcSeriesContinuationDraftService:
    """Creates unsaved authoring drafts after a fixed series concludes."""

    def __init__(
        self,
        *,
        series_repository: ArcSeriesRepositoryPort,
        character_repository: CharacterRepositoryPort,
        adapter: ArcSeriesContinuationDraftPort,
        story_arc_repository: StoryArcRepositoryPort | None = None,
        story_event_repository: StoryEventRepositoryPort | None = None,
        memory_repository: MemoryRepositoryPort | None = None,
    ) -> None:
        self._series_repository = series_repository
        self._character_repository = character_repository
        self._adapter = adapter
        self._story_arc_repository = story_arc_repository
        self._story_event_repository = story_event_repository
        self._memory_repository = memory_repository

    async def draft_next_season(
        self,
        *,
        series_id: str,
        character_id: str,
        user_id: str,
        operator_primary_language: str = "zh-TW",
        instruction: str = "",
        selected_memory_ids: list[str] | tuple[str, ...] = (),
    ) -> TemplateDraft | None:
        character = await self._character_repository.get(character_id)
        if character is None or character.user_id != user_id:
            raise ArcSeriesNotFoundError(f"Character {character_id!r} not found")

        series = await self._series_repository.get_for_user(
            series_id, user_id=user_id,
        )
        if series is None:
            raise ArcSeriesNotFoundError(f"Arc series {series_id!r} not found")

        progress = await self._series_repository.get_progress(
            character_id, series_id,
        )
        if progress is None or progress.status != SERIES_STATUS_CONCLUDED:
            raise ArcSeriesValidationError(
                "Arc series must be concluded before drafting the next season",
            )

        completed_arcs = await self._completed_series_arcs(character_id, series)
        realized_events = await self._realized_events(character_id, completed_arcs)
        memories = await self._memories(
            character_id,
            selected_memory_ids=selected_memory_ids,
        )
        return await self._adapter.draft(
            ArcSeriesContinuationContext(
                character=character,
                series=series,
                progress=progress,
                completed_arcs=tuple(completed_arcs),
                realized_events=tuple(realized_events),
                memories=tuple(memories),
                operator_primary_language=operator_primary_language,
                instruction=instruction.strip(),
            ),
        )

    async def _completed_series_arcs(
        self, character_id: str, series,
    ) -> list[StoryArc]:
        if self._story_arc_repository is None:
            return []
        arcs = await self._story_arc_repository.list_for_character(character_id)
        member_order = {
            template_id: index
            for index, template_id in enumerate(series.member_template_ids)
        }
        completed = [
            arc for arc in arcs
            if arc.status == ARC_COMPLETED
            and arc.source_template_id in member_order
        ]
        completed.sort(
            key=lambda arc: member_order.get(arc.source_template_id or "", 9999),
        )
        return completed

    async def _realized_events(
        self,
        character_id: str,
        completed_arcs: list[StoryArc],
    ) -> list[StoryEvent]:
        if self._story_event_repository is None or not completed_arcs:
            return []
        beat_ids = {
            beat.id
            for arc in completed_arcs
            for beat in arc.beats
            if beat.realized_event_id or beat.status == BEAT_REALIZED
        }
        if not beat_ids:
            return []
        events = await self._story_event_repository.list_recent(
            character_id, limit=100,
        )
        return [
            event for event in events
            if event.arc_beat_id in beat_ids
        ][:30]

    async def _memories(
        self,
        character_id: str,
        *,
        selected_memory_ids: list[str] | tuple[str, ...],
    ) -> list[MemoryItem]:
        if self._memory_repository is None:
            return []
        selected: list[MemoryItem] = []
        seen: set[str] = set()
        for memory_id in selected_memory_ids:
            memory = await self._memory_repository.get(memory_id)
            if memory is None or memory.character_id != character_id:
                continue
            selected.append(memory)
            seen.add(memory.id)
        recent = await self._memory_repository.query(
            character_id,
            kinds=[
                MemoryKind.EPISODIC,
                MemoryKind.RELATIONSHIP,
                MemoryKind.RELATIONSHIP_MILESTONE,
                MemoryKind.REFLECTION,
            ],
            min_salience=0.2,
            limit=12,
        )
        return [
            *selected,
            *(memory for memory in recent if memory.id not in seen),
        ][:20]


__all__ = ["ArcSeriesContinuationDraftService"]
