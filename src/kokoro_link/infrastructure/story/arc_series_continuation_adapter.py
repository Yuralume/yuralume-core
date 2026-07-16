"""LLM adapter for ArcSeries next-season authoring drafts."""

from __future__ import annotations

import json
import logging

from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
    extract_llm_json,
    template_draft_from_llm_json,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_ARC_CONTINUATION_DRAFT,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.arc_series_continuation import (
    ArcSeriesContinuationContext,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)
_MAX_TEXT = 1800


class LLMArcSeriesContinuationDraftAdapter:
    """Semantic bridge from concluded runtime context to authoring draft."""

    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str = FEATURE_ARC_CONTINUATION_DRAFT,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def draft(
        self, context: ArcSeriesContinuationContext,
    ) -> TemplateDraft | None:
        if await self._resolver.is_fake():
            return None
        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("arc-series continuation LLM call failed")
            return None
        data = extract_llm_json(raw)
        if not isinstance(data, dict):
            return None
        return template_draft_from_llm_json(data)


def _build_prompt(context: ArcSeriesContinuationContext) -> str:
    body = get_default_loader().render(
        "story/arc_series_continuation_draft",
        character_json=json.dumps(
            _character_payload(context.character),
            ensure_ascii=False,
            indent=2,
        ),
        series_json=json.dumps(
            _series_payload(context),
            ensure_ascii=False,
            indent=2,
        ),
        completed_arcs_json=json.dumps(
            [_arc_payload(arc) for arc in context.completed_arcs],
            ensure_ascii=False,
            indent=2,
        ),
        realized_events_json=json.dumps(
            [_event_payload(event) for event in context.realized_events],
            ensure_ascii=False,
            indent=2,
        ),
        memories_json=json.dumps(
            [_memory_payload(memory) for memory in context.memories],
            ensure_ascii=False,
            indent=2,
        ),
        instruction=context.instruction or "(none)",
    )
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _character_payload(character: Character) -> dict:
    return {
        "id": character.id,
        "name": character.name,
        "summary": character.summary,
        "personality": list(character.personality)[:8],
        "interests": list(character.interests)[:8],
        "speaking_style": character.speaking_style,
        "aspirations": list(character.aspirations)[:8],
        "world_frame": character.world_frame,
    }


def _series_payload(context: ArcSeriesContinuationContext) -> dict:
    series = context.series
    return {
        "id": series.id,
        "title": series.title,
        "premise": series.premise,
        "theme": series.theme,
        "tone": series.tone,
        "member_template_ids": list(series.member_template_ids),
        "progress": {
            "status": context.progress.status,
            "current_index": context.progress.current_index,
            "last_arc_id": context.progress.last_arc_id,
        },
    }


def _arc_payload(arc: StoryArc) -> dict:
    return {
        "id": arc.id,
        "title": arc.title,
        "premise": arc.premise,
        "theme": arc.theme,
        "tone": arc.tone,
        "source_template_id": arc.source_template_id,
        "beats": [_beat_payload(beat) for beat in arc.beats],
    }


def _beat_payload(beat: StoryArcBeat) -> dict:
    return {
        "sequence": beat.sequence,
        "title": beat.title,
        "summary": _truncate(beat.summary, 600),
        "tension": beat.tension,
        "status": beat.status,
        "realized_event_id": beat.realized_event_id,
        "scene_type": beat.scene_type,
        "location": beat.location,
        "dramatic_question": beat.dramatic_question,
    }


def _event_payload(event: StoryEvent) -> dict:
    return {
        "date": event.date,
        "arc_beat_id": event.arc_beat_id,
        "narrative": _truncate(event.narrative, _MAX_TEXT),
        "emotional_tone": event.emotional_tone,
    }


def _memory_payload(memory: MemoryItem) -> dict:
    return {
        "id": memory.id,
        "kind": str(memory.kind),
        "content": _truncate(memory.content, _MAX_TEXT),
        "salience": memory.salience,
        "tags": list(memory.tags),
    }


def _truncate(text: str, limit: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n[truncated]"


__all__ = ["LLMArcSeriesContinuationDraftAdapter"]

