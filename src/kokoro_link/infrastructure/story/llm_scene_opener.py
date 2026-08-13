"""LLM writer for the opening of a player-pulled story scene (SC1-A).

One call produces everything the player sees when 起幕 fires: the scene
frame (title / location / mood), the narration that pulls the curtain
back, and the character's first performed line.

**No synthesized fallback, on purpose.** Every other story writer in this
package degrades to a deterministic ``Null*`` scene when the model is
unavailable, because those run in the background and a placeholder beats
a stalled arc. This one runs in the foreground of a button the hosted
player is charged a fixed price for (red line 2),
so a template-assembled stand-in would mean charging for filler. Both
implementations therefore answer ``None`` when they cannot produce a real
opening, and the service turns that into a failed action that writes
nothing and charges nothing.
"""

from __future__ import annotations

import json
import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story_scene import (
    StorySceneOpeningContext,
    StorySceneOpeningDraft,
    StorySceneOpenerPort,
)
from kokoro_link.domain.services.story_tone_policy import resolve_prompt_tone
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.operator_scene_position import (
    render_opener_operator_position_block,
)
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.infrastructure.story.date_context import (
    render_story_date_context_block,
)


_LOGGER = logging.getLogger(__name__)

_MAX_NARRATION_CHARS = 700
_MAX_LINE_CHARS = 500
_MAX_TITLE_CHARS = 60
_MAX_LABEL_CHARS = 40

_UNSPECIFIED = "（未指定）"


class NullStorySceneOpener(StorySceneOpenerPort):
    """Null object for wiring without an LLM. Always fails the opening.

    Kept as a real implementation rather than ``None`` so the service can
    depend on the port unconditionally; see the module docstring for why
    it does not fabricate a scene.
    """

    async def write_opening(
        self, context: StorySceneOpeningContext,
    ) -> StorySceneOpeningDraft | None:
        _LOGGER.info(
            "story scene opener not configured; refusing to open a scene "
            "character=%s layer=%s",
            context.character.id,
            context.material.layer,
        )
        return None


class LLMStorySceneOpener(StorySceneOpenerPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
        cloud_mode: bool = False,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )
        # GF6 — hosted tone policy. Default ``False`` keeps self-host
        # prompts byte-identical (see ``domain.services.story_tone_policy``).
        self._cloud_mode = cloud_mode

    async def write_opening(
        self, context: StorySceneOpeningContext,
    ) -> StorySceneOpeningDraft | None:
        # A fake provider is a *test / unconfigured* backend, not a
        # degraded one. Letting it through would put obviously synthetic
        # text on the player's stage and — hosted — bill for it.
        if await self._resolver.is_fake(character=context.character):
            _LOGGER.info(
                "story scene opener resolved a fake model; refusing to open "
                "a scene character=%s",
                context.character.id,
            )
            return None

        try:
            raw = await self._resolver.generate(
                _build_prompt(context, cloud_mode=self._cloud_mode),
                character=context.character,
            )
        except Exception:
            _LOGGER.exception(
                "story scene opener LLM call failed character=%s layer=%s",
                context.character.id,
                context.material.layer,
            )
            return None

        parsed = _parse_payload(raw)
        if parsed is None:
            _LOGGER.warning(
                "story scene opener returned unparseable output character=%s",
                context.character.id,
            )
            return None

        narration = _clean(parsed.get("narration"), _MAX_NARRATION_CHARS)
        character_line = _clean(parsed.get("character_line"), _MAX_LINE_CHARS)
        # Both halves are load-bearing: narration alone is a scene nobody
        # is in, a line alone is indistinguishable from ordinary chat.
        # Half an opening is a failed opening.
        if not narration or not character_line:
            _LOGGER.warning(
                "story scene opener produced an incomplete opening "
                "character=%s narration=%s line=%s",
                context.character.id,
                bool(narration),
                bool(character_line),
            )
            return None

        return StorySceneOpeningDraft(
            narration=narration,
            character_line=character_line,
            title=_clean(parsed.get("title"), _MAX_TITLE_CHARS),
            # The frame labels are decoration; a model that skipped them
            # still produced a playable scene, so they fall back to the
            # material rather than failing the action.
            location=(
                _clean(parsed.get("location"), _MAX_LABEL_CHARS)
                or context.material.location
            ),
            mood=_clean(parsed.get("mood"), _MAX_LABEL_CHARS) or None,
        )


def _build_prompt(
    context: StorySceneOpeningContext, *, cloud_mode: bool = False,
) -> str:
    material = context.material
    character = context.character
    scene_characters = (
        "、".join(material.scene_characters)
        if material.scene_characters
        else _UNSPECIFIED
    )
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    body = get_default_loader().render(
        "story/scene_opener",
        character_name=character.name,
        character_summary=character.summary or _UNSPECIFIED,
        identity_block="\n".join(render_character_identity_lines(character)),
        speaking_style=character.speaking_style or "自然",
        world_frame=character.world_frame or "modern",
        today=context.today.isoformat(),
        date_context_block=render_story_date_context_block(
            context.today,
            language_tag=context.operator_primary_language,
            include_today_line=False,
        ),
        material_layer=material.layer,
        arc_title=material.arc_title or _UNSPECIFIED,
        arc_premise=material.arc_premise or _UNSPECIFIED,
        # GF6: hosted, ``mature`` renders as ``dramatic``. Blank stays
        # blank so the "（未指定）" fallback below is untouched.
        arc_tone=resolve_prompt_tone(
            material.arc_tone, cloud_mode=cloud_mode, context="scene opener",
        ) or _UNSPECIFIED,
        material_title=material.title,
        material_summary=material.summary or _UNSPECIFIED,
        material_tension=material.tension or _UNSPECIFIED,
        material_scene_type=material.scene_type or _UNSPECIFIED,
        material_location=material.location or _UNSPECIFIED,
        material_scene_characters=scene_characters,
        companion_block=_companion_block(context),
        material_dramatic_question=material.dramatic_question or _UNSPECIFIED,
        operator_position_block=render_opener_operator_position_block(
            material.operator_position, material.operator_note,
        ),
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _companion_block(context: StorySceneOpeningContext) -> str:
    """Companion metadata for the labels this scene actually casts.

    Same shape the autonomous beat scene writer feeds its prompt, so the
    two writers describe the same supporting cast the same way.
    """
    labels = {
        label.casefold() for label in context.material.scene_characters
    }
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
    return (
        "\n".join(lines)
        if lines
        else "（沒有對上的 companion metadata；可把出場人物當 NPC label 使用）"
    )


def _parse_payload(raw: str) -> dict | None:
    payload = _extract_json_object(raw)
    if payload is None:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value.strip())
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


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
