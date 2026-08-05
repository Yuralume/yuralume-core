"""LLM adapter turning a ready fusion story into an arc-template draft."""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterable

from kokoro_link.application.services.arc_template_intake_service import (
    BeatDraft,
    TemplateDraft,
    extract_llm_json,
    template_draft_from_llm_json,
)
from kokoro_link.application.services.feature_keys import FEATURE_ARC_ADAPT
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.fusion_to_arc import (
    DEFAULT_FUSION_OPERATOR_MODE,
    FUSION_OPERATOR_MODE_OBSERVER,
    FUSION_OPERATOR_MODE_UNCHANGED,
    FUSION_OPERATOR_MODE_WRITE_IN,
    FusionToArcContext,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import FusionStory, FusionStoryBeat
from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_PRESENT,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)
_MAX_STORY_TEXT_CHARS = 14000
# Relationship facts arrive pre-rendered from the service (one label per
# line). The clamp only guards the prompt against a pathological seed —
# same budget as the arc planner's twin block.
_MAX_OPERATOR_RELATIONSHIP_LINE_CHARS = 200

# OP1-C 拍板 #2 — the creator's answer to "how do I enter this story"
# selects which instruction block the model reads. The *branching* lives
# here in Python and the *wording* lives in the prompt pack, which is the
# division this loader was built for; it also means the write-in rewrite
# licence is physically absent from the prompt in the other two modes,
# rather than present-but-overridden (紅線 5).
_MODE_PROMPT_NAMES: dict[str, str] = {
    FUSION_OPERATOR_MODE_WRITE_IN: "fusion/adapt_operator_write_in",
    FUSION_OPERATOR_MODE_OBSERVER: "fusion/adapt_operator_observer",
    FUSION_OPERATOR_MODE_UNCHANGED: "fusion/adapt_operator_unchanged",
}

# The position every beat carries when the creator's choice already
# answered the question for the whole story. ``write_in`` is absent from
# this map on purpose: that is the one mode where the answer differs per
# beat, so it is the model's judgement to make (LLM-first) and nothing
# here overwrites it.
_MODE_FORCED_POSITIONS: dict[str, str] = {
    FUSION_OPERATOR_MODE_OBSERVER: OPERATOR_POSITION_PRESENT,
    FUSION_OPERATOR_MODE_UNCHANGED: OPERATOR_POSITION_ABSENT,
}


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
        # Resolved once so the prompt the model reads and the rule applied
        # to what it answers can never disagree about which mode this is.
        mode = _resolve_mode(context.operator_mode)
        prompt = _build_prompt(context, mode)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("fusion-to-arc LLM call failed")
            return None
        data = extract_llm_json(raw)
        if not isinstance(data, dict):
            return None
        draft = template_draft_from_llm_json(data)
        if draft is None:
            return None
        return _apply_operator_mode(draft, mode)


def _resolve_mode(mode: str) -> str:
    """The mode this call actually runs under.

    An unknown value cannot reach here through the API (the request DTO
    rejects it) — but this adapter is also a library seam, so it lands on
    the plan's protocol default rather than raising mid-generation. That
    default is the conservative branch: the one that leaves the creator's
    story alone.
    """
    if mode in _MODE_PROMPT_NAMES:
        return mode
    _LOGGER.warning(
        "fusion-to-arc: unknown operator mode %r — adapting as %r",
        mode, DEFAULT_FUSION_OPERATOR_MODE,
    )
    return DEFAULT_FUSION_OPERATOR_MODE


def _build_prompt(context: FusionToArcContext, mode: str) -> str:
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
        operator_mode_block=get_default_loader().render(
            _MODE_PROMPT_NAMES[mode],
        ),
        operator_relationship_block=_render_operator_relationship_block(
            context.operator_relationship_lines,
            mode,
        ),
    )
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _render_operator_relationship_block(
    lines: tuple[str, ...], mode: str,
) -> str:
    """Who the player is to this cast, when the mode needs to know.

    The facts are pre-rendered by the service (application layer owns
    *which* facts travel); the caption explaining what to do with them
    belongs here, next to the ``operator_position`` schema it serves.

    Empty tuple renders the empty string — no heading, no placeholder. A
    section titled "the player's relationship" with nothing under it is a
    hole the model feels obliged to fill, and it would fill it by
    inventing a relationship nobody agreed to.

    ``unchanged`` renders nothing regardless of what it was handed. The
    service already declines to load these facts under that mode, and
    this repeats the rule at the prompt boundary rather than trusting it:
    a story the creator declared they are not in must not have their name
    in the prompt at all, and 紅線 5 is worth two lines of duplication.
    """
    if mode == FUSION_OPERATOR_MODE_UNCHANGED:
        return ""
    entries: list[str] = []
    for raw in lines:
        cleaned = " ".join(str(raw).split())
        if not cleaned:
            continue
        if len(cleaned) > _MAX_OPERATOR_RELATIONSHIP_LINE_CHARS:
            cleaned = cleaned[:_MAX_OPERATOR_RELATIONSHIP_LINE_CHARS] + "…"
        entries.append(cleaned)
    if not entries:
        return ""
    return "\n".join([
        "Who the player is to this cast (confirmed by the creator when "
        "each character was created):",
        *entries,
        "Use these when you decide operator_position and write "
        "operator_note: address the player the way the character already "
        "addresses them, and do not invent a closeness these facts do not "
        "support. Shared history that is not written here has not "
        "happened.",
    ]) + "\n\n"


def _apply_operator_mode(draft: TemplateDraft, mode: str) -> TemplateDraft:
    """Make the creator's choice true of the draft, not merely requested.

    Two of the three modes answer the player's place for the whole story
    up front, so the beats they produce are not a judgement call the model
    gets to revise: 「我旁觀」 means the player is a witness to all of it and
    「保持原樣」 means they are in none of it. Enforcing that here is not a
    semantic patch on the model's output — nothing inspects the prose, and
    no keyword decides anything — it is the difference between a declared
    creator intent and a request the model may have drifted from.

    ``write_in`` returns the draft untouched: there the whole point is
    that the model judges each beat, and overwriting that would be this
    function inventing positions nobody chose.
    """
    forced = _MODE_FORCED_POSITIONS.get(mode)
    if forced is None:
        return draft
    drop_notes = mode == FUSION_OPERATOR_MODE_UNCHANGED
    beats = tuple(
        dataclasses.replace(
            beat,
            operator_position=forced,
            # A note is prose about the player's dramatic place. In a
            # story the creator declared they are not in, one can only be
            # a leak from an instruction the model was told to ignore.
            operator_note=None if drop_notes else beat.operator_note,
        )
        for beat in draft.beats
    )
    if _positions_of(beats) != _positions_of(draft.beats):
        _LOGGER.info(
            "fusion-to-arc: normalised %d beat positions to %r for mode %r",
            len(beats), forced, mode,
        )
    return dataclasses.replace(draft, beats=beats)


def _positions_of(beats: Iterable[BeatDraft]) -> tuple[str | None, ...]:
    return tuple(beat.operator_position for beat in beats)


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
