"""LLM adapter turning a ready fusion story into an arc-template draft."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
    extract_llm_json,
    template_draft_from_llm_json,
)
from kokoro_link.application.services.feature_keys import FEATURE_ARC_ADAPT
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.fusion_to_arc import FusionToArcContext
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import FusionStory, FusionStoryBeat
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)
_MAX_STORY_TEXT_CHARS = 14000


class LLMFusionToArcAdapter:
    """Semantic adaptation layer from read-only prose to playable beats."""

    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str = FEATURE_ARC_ADAPT,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def adapt(self, context: FusionToArcContext) -> TemplateDraft | None:
        if await self._resolver.is_fake():
            return None
        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("fusion-to-arc LLM call failed")
            return None
        data = extract_llm_json(raw)
        if not isinstance(data, dict):
            return None
        return template_draft_from_llm_json(data)


def _build_prompt(context: FusionToArcContext) -> str:
    story_payload = _story_payload(context.story)
    character_payload = [_character_payload(c) for c in context.characters]
    body = get_default_loader().render(
        "fusion/adapt_to_arc",
        story_json=json.dumps(story_payload, ensure_ascii=False, indent=2),
        characters_json=json.dumps(
            character_payload,
            ensure_ascii=False,
            indent=2,
        ),
        instruction=context.instruction.strip() or "(none)",
    )
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _story_payload(story: FusionStory) -> dict:
    return {
        "id": story.id,
        "title": story.title,
        "premise": story.premise,
        "theme": story.theme,
        "operator_prompt": story.prompt,
        "full_text": _truncate(story.joined_text(), _MAX_STORY_TEXT_CHARS),
        "beats": [_beat_payload(beat) for beat in story.beats],
    }


def _beat_payload(beat: FusionStoryBeat) -> dict:
    return {
        "sequence": beat.sequence,
        "act": beat.act,
        "title": beat.title,
        "hook": beat.hook,
        "dramatic_question": beat.dramatic_question,
        "content": _truncate(beat.content, 2600),
        "focus_character_ids": list(beat.focus_character_ids),
    }


def _character_payload(character: Character) -> dict:
    return {
        "id": character.id,
        "name": character.name,
        "summary": character.summary,
        "personality": _limit_list(character.personality, 8),
        "interests": _limit_list(character.interests, 8),
        "speaking_style": character.speaking_style,
        "boundaries": _limit_list(character.boundaries, 8),
        "aspirations": _limit_list(character.aspirations, 8),
        "world_frame": character.world_frame,
    }


def _limit_list(values: Iterable[str], limit: int) -> list[str]:
    return [v for v in values if v][:limit]


def _truncate(text: str, limit: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n[truncated]"
