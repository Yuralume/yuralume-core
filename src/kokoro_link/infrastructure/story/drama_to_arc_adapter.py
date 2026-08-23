"""LLM adapter turning a played drama path into an arc-template draft.

BD7. The output schema, the beat constraints and the three player-mode
blocks are exactly the fusion adaptation's — the destination is the same
:class:`TemplateDraft` and the same reviewing wizard — so this adapter
renders the *same frozen prompt templates* rather than forking them
(``src/kokoro_link/data/prompts`` is a frozen baseline pack; branching
belongs in code).

What differs is the source, and that difference is stated in a code-side
preamble ahead of the rendered body — the established pattern in this
subsystem (``branching_drama_planner._with_prompt_preamble``,
``drama_operator_position_lines``). A playthrough is not prose somebody
wrote about characters: it is a transcript with the player inside it,
and an adaptation that mistook the player's own lines for authored
narration would hand the arc back beats the player has to watch
themselves perform.
"""

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
from kokoro_link.contracts.drama_to_arc import (
    DramaPathBeat,
    DramaToArcContext,
)
from kokoro_link.contracts.fusion_to_arc import (
    DEFAULT_FUSION_OPERATOR_MODE,
    FUSION_OPERATOR_MODE_OBSERVER,
    FUSION_OPERATOR_MODE_UNCHANGED,
    FUSION_OPERATOR_MODE_WRITE_IN,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.branching_drama import BranchingDrama
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_PRESENT,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_NARRATION_CHARS = 2200
_MAX_EXCHANGE_CHARS = 900
_MAX_EXCHANGES_PER_BEAT = 8
_MAX_OPERATOR_RELATIONSHIP_LINE_CHARS = 200

# Same three blocks the fusion conversion reads — the question ("how do I
# enter the arc this becomes?") and its three answers are identical, and
# forking the wording would let the two paths drift apart on a decision
# the player experiences as one choice.
_MODE_PROMPT_NAMES: dict[str, str] = {
    FUSION_OPERATOR_MODE_WRITE_IN: "fusion/adapt_operator_write_in",
    FUSION_OPERATOR_MODE_OBSERVER: "fusion/adapt_operator_observer",
    FUSION_OPERATOR_MODE_UNCHANGED: "fusion/adapt_operator_unchanged",
}

# ``write_in`` is absent on purpose (as on the fusion side): that is the
# one mode where the player's place differs per beat, so it is the
# model's judgement and nothing here overwrites it.
_MODE_FORCED_POSITIONS: dict[str, str] = {
    FUSION_OPERATOR_MODE_OBSERVER: OPERATOR_POSITION_PRESENT,
    FUSION_OPERATOR_MODE_UNCHANGED: OPERATOR_POSITION_ABSENT,
}

_SOURCE_PREAMBLE = (
    "本次改編的來源不是別人寫好的小說，而是一段**玩家實際走完的分歧劇場"
    "紀錄**：下面 JSON 的 `path` 是他在分歧樹上真正走過的那一條線，依遊玩"
    "順序排列。\n"
    "- 每一段的 `title` / `summary` 是這一幕原本被規劃成什麼；"
    "`narration` 是玩家走進來時真正演出的內容。\n"
    "- `exchanges` 是他在這一段裡與角色來回的對戲，"
    "也就是他為了把戲推到下一段所說的話。\n"
    "- `tone` 是這一段被選中的分歧取向（dark / sunny / neutral）。\n"
    "**玩家講的話是玩家的，不是角色的台詞，也不是敘事**：改編時把它讀成"
    "「這個人在這段戲裡想做什麼」，用來判斷他在往後的 arc 裡站在哪裡，"
    "不要把它照抄成角色要說的話。\n"
    "沒有走到的分支不存在於這段故事裡，不要補寫玩家沒走過的路。"
)


class LLMDramaToArcAdapter:
    """Semantic adaptation layer from a walked path to playable beats."""

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

    async def adapt(self, context: DramaToArcContext) -> TemplateDraft | None:
        if await self._resolver.is_fake():
            return None
        # Resolved once so the prompt the model reads and the rule applied
        # to what it answers can never disagree about which mode this is.
        mode = _resolve_mode(context.operator_mode)
        prompt = _build_prompt(context, mode)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("drama-to-arc LLM call failed")
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
    rejects it, and the service pre-fills from the drama's own position),
    but this adapter is also a library seam — so it lands on the
    conservative branch rather than raising mid-generation.
    """
    if mode in _MODE_PROMPT_NAMES:
        return mode
    _LOGGER.warning(
        "drama-to-arc: unknown operator mode %r — adapting as %r",
        mode, DEFAULT_FUSION_OPERATOR_MODE,
    )
    return DEFAULT_FUSION_OPERATOR_MODE


def _build_prompt(context: DramaToArcContext, mode: str) -> str:
    loader = get_default_loader()
    body = loader.render(
        "fusion/adapt_to_arc",
        story_json=json.dumps(
            _drama_payload(context.drama, context.path),
            ensure_ascii=False,
            indent=2,
        ),
        characters_json=json.dumps(
            [_character_payload(c) for c in context.characters],
            ensure_ascii=False,
            indent=2,
        ),
        instruction=context.instruction.strip() or "(none)",
        operator_mode_block=loader.render(_MODE_PROMPT_NAMES[mode]),
        operator_relationship_block=_render_operator_relationship_block(
            context.operator_relationship_lines, mode,
        ),
    )
    parts = [_SOURCE_PREAMBLE, body]
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    if language_hint:
        parts.insert(0, language_hint)
    return "\n\n".join(parts)


def _render_operator_relationship_block(
    lines: tuple[str, ...], mode: str,
) -> str:
    """Who the player is to this cast, when the mode needs to know.

    Empty tuple renders the empty string — no heading, no placeholder: a
    section titled "the player's relationship" with nothing under it is a
    hole the model feels obliged to fill, and it would fill it by
    inventing a relationship nobody agreed to. ``unchanged`` renders
    nothing regardless of what it was handed (紅線 5), repeating the
    service's rule at the prompt boundary rather than trusting it.
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
    """Make the player's choice true of the draft, not merely requested.

    Same rule as the fusion twin: two of the three modes answer the
    player's place for the whole story up front, so the beats they produce
    are not a judgement the model gets to revise. Nothing inspects prose
    and no keyword decides anything — this is the difference between a
    declared intent and a request the model may have drifted from.
    """
    forced = _MODE_FORCED_POSITIONS.get(mode)
    if forced is None:
        return draft
    drop_notes = mode == FUSION_OPERATOR_MODE_UNCHANGED
    beats = tuple(
        dataclasses.replace(
            beat,
            operator_position=forced,
            operator_note=None if drop_notes else beat.operator_note,
        )
        for beat in draft.beats
    )
    if _positions_of(beats) != _positions_of(draft.beats):
        _LOGGER.info(
            "drama-to-arc: normalised %d beat positions to %r for mode %r",
            len(beats), forced, mode,
        )
    return dataclasses.replace(draft, beats=beats)


def _positions_of(beats: Iterable[BeatDraft]) -> tuple[str | None, ...]:
    return tuple(beat.operator_position for beat in beats)


def _drama_payload(
    drama: BranchingDrama, path: tuple[DramaPathBeat, ...],
) -> dict:
    return {
        "id": drama.id,
        "title": drama.title,
        "operator_prompt": drama.prompt,
        "total_segments": drama.total_segments,
        # The framing the whole tree was planned against. Carried as a
        # fact about the source, not as an instruction: what the *arc*
        # does with the player is said by the mode block alone.
        "played_operator_position": drama.operator_position,
        "played_operator_note": drama.operator_note or "",
        "path": [_path_beat_payload(beat) for beat in path],
    }


def _path_beat_payload(beat: DramaPathBeat) -> dict:
    return {
        "sequence": beat.sequence,
        "depth": beat.depth,
        "tone": beat.tone,
        "title": beat.title,
        "summary": beat.summary,
        "narration": _truncate(beat.narration, _MAX_NARRATION_CHARS),
        "exchanges": [
            {
                "player_input": _truncate(ex.player_input, _MAX_EXCHANGE_CHARS),
                "response": _truncate(ex.response, _MAX_EXCHANGE_CHARS),
            }
            for ex in beat.exchanges[:_MAX_EXCHANGES_PER_BEAT]
        ],
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
