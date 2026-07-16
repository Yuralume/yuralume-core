"""LLM-backed writer for autonomous story-arc beat scenes."""

from __future__ import annotations

import json
import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story_arc import (
    StoryBeatSceneContext,
    StoryBeatSceneDraft,
    StoryBeatSceneWriterPort,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_MAX_NARRATIVE_CHARS = 800
_MAX_NOTE_CHARS = 180
_ALLOWED_TONES = {
    "peaceful", "happy", "melancholy", "lonely", "curious",
    "excited", "anxious", "nostalgic", "tired", "content",
    "restless", "hopeful", "tense", "bittersweet",
}
_ALLOWED_CAST_STRATEGIES = {
    "inner_monologue",
    "npc_dialogue",
    "companion_dialogue",
    "user_present",
    "autonomous",
}


class _NullScenePack:
    """Localized sentence templates for the model-free fallback scene.

    LLM-FIRST EXEMPTION: this is the deterministic path used for the
    fake provider AND every LLM-failure branch (there is no model call
    to carry the operator-language fact into). Mirrors the exemption in
    ``llm_arc_planner._synthetic_arc`` — the player-visible scaffolding
    is hardcoded per shipped locale, with the beat's own title/summary/
    location interpolated verbatim. This is not keyword-branching
    business logic; it is a static placeholder the next real LLM scene
    supersedes. New shipped languages get a new entry below.

    Fields:
      * ``location_known`` / ``location_unknown``: the leading locative
        clause, ``{location}`` filled from the beat.
      * ``with_others``: NPC-present narrative; ``{location} {others}
        {title} {summary}`` interpolated.
      * ``solo``: inner-monologue narrative; ``{location} {title}
        {summary}``.
      * ``joiner``: how ``scene_characters`` names are joined.
    """

    __slots__ = (
        "location_known", "location_unknown",
        "with_others", "solo", "joiner",
    )

    def __init__(
        self,
        *,
        location_known: str,
        location_unknown: str,
        with_others: str,
        solo: str,
        joiner: str,
    ) -> None:
        self.location_known = location_known
        self.location_unknown = location_unknown
        self.with_others = with_others
        self.solo = solo
        self.joiner = joiner


_NULL_SCENE_PACKS: dict[str, _NullScenePack] = {
    "zh-TW": _NullScenePack(
        location_known="在{location}",
        location_unknown="在熟悉的地方",
        with_others="{location}，我和{others}把「{title}」這件事說開。{summary}",
        solo="{location}，我獨自面對「{title}」。{summary}",
        joiner="、",
    ),
    "en-US": _NullScenePack(
        location_known="At {location}",
        location_unknown="Somewhere familiar",
        with_others=(
            "{location}, {others} and I finally talk through \"{title}\". "
            "{summary}"
        ),
        solo="{location}, I face \"{title}\" on my own. {summary}",
        joiner=", ",
    ),
    "ja-JP": _NullScenePack(
        location_known="{location}で",
        location_unknown="いつもの場所で",
        with_others=(
            "{location}、私は{others}と「{title}」のことを話し合った。{summary}"
        ),
        solo="{location}、私はひとりで「{title}」に向き合う。{summary}",
        joiner="、",
    ),
}
_NULL_SCENE_FALLBACK_LANGUAGE = "zh-TW"


def _null_scene_pack(language_tag: str | None) -> _NullScenePack:
    """Exact tag → language-subtag family → zh-TW (same rule as the
    arc planner's synthetic template resolution)."""
    tag = (language_tag or "").strip()
    if tag in _NULL_SCENE_PACKS:
        return _NULL_SCENE_PACKS[tag]
    subtag = tag.split("-", 1)[0].lower() if tag else ""
    if subtag:
        for known, pack in _NULL_SCENE_PACKS.items():
            if known.split("-", 1)[0].lower() == subtag:
                return pack
    return _NULL_SCENE_PACKS[_NULL_SCENE_FALLBACK_LANGUAGE]


class NullStoryBeatSceneWriter(StoryBeatSceneWriterPort):
    """Deterministic fallback used for fake-provider and LLM failures."""

    async def write_scene(
        self, context: StoryBeatSceneContext,
    ) -> StoryBeatSceneDraft:
        beat = context.beat
        pack = _null_scene_pack(context.operator_primary_language)
        location = (
            pack.location_known.format(location=beat.location)
            if beat.location
            else pack.location_unknown
        )
        others = pack.joiner.join(beat.scene_characters)
        if others:
            narrative = pack.with_others.format(
                location=location,
                others=others,
                title=beat.title,
                summary=beat.summary,
            )
            cast_strategy = "npc_dialogue"
        else:
            narrative = pack.solo.format(
                location=location,
                title=beat.title,
                summary=beat.summary,
            )
            cast_strategy = "inner_monologue"
        return StoryBeatSceneDraft(
            narrative=_clean_text(narrative, _MAX_NARRATIVE_CHARS),
            emotional_tone=None,
            cast_strategy=cast_strategy,
            participation_note="null fallback; no user dependency",
        )


class LLMStoryBeatSceneWriter(StoryBeatSceneWriterPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def write_scene(
        self, context: StoryBeatSceneContext,
    ) -> StoryBeatSceneDraft:
        async def _fallback() -> StoryBeatSceneDraft:
            return await NullStoryBeatSceneWriter().write_scene(context)

        if await self._resolver.is_fake(character=context.character):
            return await _fallback()

        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(
                prompt,
                character=context.character,
            )
        except Exception:
            _LOGGER.exception(
                "story beat scene writer LLM call failed beat=%s",
                context.beat.id,
            )
            return await _fallback()

        payload = _extract_json_object(raw)
        if payload is None:
            return await _fallback()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return await _fallback()
        if not isinstance(parsed, dict):
            return await _fallback()

        narrative = _clean_text(parsed.get("narrative"), _MAX_NARRATIVE_CHARS)
        if not narrative:
            return await _fallback()
        tone = _normalise_tone(parsed.get("emotional_tone"))
        cast_strategy = _normalise_cast_strategy(parsed.get("cast_strategy"))
        note = _clean_text(parsed.get("participation_note"), _MAX_NOTE_CHARS)
        return StoryBeatSceneDraft(
            narrative=narrative,
            emotional_tone=tone,
            cast_strategy=cast_strategy,
            participation_note=note,
        )


def _build_prompt(context: StoryBeatSceneContext) -> str:
    beat = context.beat
    arc = context.arc
    scene_characters = (
        "、".join(beat.scene_characters) if beat.scene_characters else "（未指定）"
    )
    attempt_lines = [
        f"- 已嘗試帶出次數：{beat.play_attempt_count}",
        f"- 上次嘗試來源：{beat.last_play_attempt_source or '（無）'}",
        f"- 上次嘗試結果：{beat.last_play_attempt_result or '（無）'}",
        f"- 上次推進力道：{beat.last_play_push_intensity or '（無）'}",
    ]
    if beat.last_play_attempt_at is not None:
        attempt_lines.insert(
            1,
            f"- 上次嘗試時間：{beat.last_play_attempt_at.isoformat()}",
        )
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    body = get_default_loader().render(
        "story/beat_scene_writer",
        character_name=context.character.name,
        character_summary=context.character.summary or "（未設定）",
        identity_block="\n".join(
            render_character_identity_lines(context.character),
        ),
        speaking_style=context.character.speaking_style or "自然",
        world_frame=context.character.world_frame or "modern",
        today=context.today.isoformat(),
        arc_title=arc.title,
        arc_premise=arc.premise,
        arc_tone=arc.tone,
        beat_title=beat.title,
        beat_summary=beat.summary,
        beat_tension=beat.tension,
        beat_scene_type=beat.scene_type,
        beat_location=beat.location or "（未指定）",
        beat_scene_characters=scene_characters,
        companion_block=_companion_block(context),
        beat_dramatic_question=beat.dramatic_question or "（未指定）",
        beat_required="是" if beat.required else "否",
        attempt_block="\n".join(attempt_lines),
        user_involvement_policy=context.user_involvement_policy,
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _companion_block(context: StoryBeatSceneContext) -> str:
    labels = {label.casefold() for label in context.beat.scene_characters}
    lines: list[str] = []
    for companion in context.character.companions:
        name = companion.name.strip()
        role = companion.role.strip()
        if not name and not role:
            continue
        if name.casefold() not in labels and role.casefold() not in labels:
            continue
        details = []
        if role:
            details.append(f"role={role}")
        if companion.brief_profile:
            details.append(f"profile={companion.brief_profile}")
        if companion.personality_sketch:
            details.append(
                "personality=" + "、".join(companion.personality_sketch),
            )
        if companion.relationship_snippet:
            details.append(f"relationship={companion.relationship_snippet}")
        suffix = f"（{'; '.join(details)}）" if details else ""
        lines.append(f"- {name}{suffix}")
    return "\n".join(lines) if lines else "（沒有對上的 companion metadata；可把 scene_characters 當 NPC label 使用）"


def _clean_text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value.strip())
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _normalise_tone(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or text == "null":
        return None
    return text if text in _ALLOWED_TONES else None


def _normalise_cast_strategy(value: object) -> str:
    if not isinstance(value, str):
        return "autonomous"
    text = value.strip().lower()
    return text if text in _ALLOWED_CAST_STRATEGIES else "autonomous"


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None
