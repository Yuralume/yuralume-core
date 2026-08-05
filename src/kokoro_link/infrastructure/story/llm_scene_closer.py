"""LLM writer for the wrap-up of a player-pulled story scene (SC1-D).

One prompt serves all three ways a scene can end, because they differ
only in framing and in who decides:

* ``resolved`` — asked after an in-scene turn: *is this scene finished?*
  Almost every call answers "no" and stops there, which is why the
  unfinished verdict is deliberately the cheapest possible reply.
* ``manual`` — the player pressed 「結束場景」. Already decided; write it.
* ``timeout`` — the player left and never came back (SC1-E). Also already
  decided, and framed from 「你離開之後，她自己…」 rather than from a
  scene the two of them ended together.

**The red line lives here, not only in the prompt** (STORY_SCENE_PLAN
§3.4 #1). The model is told never to describe a player action the
transcript does not show — and it is also required to *cite* the
transcript line behind any player action it does describe. This module
checks those citations against the lines the player really produced and
throws the whole draft away when one of them is invented. It is not a
content filter and does not read the prose for banned phrases (that
would be the keyword special-casing the LLM-first rule bans); it is a
provenance check, the same shape as every other "quote your source"
seam in this codebase.

The check has one absolute case and one partial one. Absolute: a scene
where the player never spoke has no player actions at all, so *any*
citation is a fabrication and the draft is refused outright. Partial: a
model that simply reports no citations is trusted, because there is
nothing left to verify against. That asymmetry is honest about what a
provenance check can and cannot do, and it is why the prompt carries the
same rule in words.

Failure is degradation, not an exception, and the direction depends on
who asked. Refer to :class:`StorySceneCloserPort` for why ``None`` means
"the scene continues" on the verdict path and "close it anyway, without
prose" on the two forced paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story_scene import (
    StorySceneClosingContext,
    StorySceneClosingDraft,
    StorySceneCloserPort,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_CLOSE_RESOLVED,
    SCENE_CLOSE_TIMEOUT,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.operator_scene_position import (
    render_closer_operator_position_block,
)
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.infrastructure.story.date_context import (
    render_story_date_context_block,
)


_LOGGER = logging.getLogger(__name__)

_MAX_NARRATION_CHARS = 700
_MAX_SUMMARY_CHARS = 500

_TIMEOUT_SECONDS = 30.0
"""Hard bound on the call, for the same reason the chips writer has one.

The verdict runs inside the conversation's turn lease, so an upstream
that accepts and never answers would hold the player's own thread shut
over a question whose usual answer is "no". Generous for a slow local
model, far below the 180s lease."""

_PLAYER_LABEL_RE = re.compile(r"^玩家\s*[：:]\s*")
"""Strips *our own* transcript label off a quotation.

Models tend to copy the line as it was shown to them, label and all.
Removing the prefix we ourselves added is parsing our own format — not a
content rule about what the player may have said."""

_UNSPECIFIED = "（未指定）"

_MODE_BLOCKS = {
    SCENE_CLOSE_RESOLVED: (
        "現在的情況：\n"
        "- 玩家還在場上，這場戲**可能**已經演到了自然的落幕點，也可能還沒。\n"
        "- 由你判斷：上面的戲劇問題是否已經在逐字稿裡得到了回答或明確的轉向？"
        "情緒是否已經走完一個完整的起落？"
    ),
    SCENE_CLOSE_MANUAL: (
        "現在的情況：\n"
        "- 玩家選擇在此打住，這場戲到這裡結束。\n"
        "- 這不是壞事也不是逃避，就是兩個人今天聊到這裡。"
        "請以「你們就此打住」的角度收，不要責怪玩家離開，"
        "也不要硬把戲劇問題回答掉——沒答完就是沒答完。"
    ),
    SCENE_CLOSE_TIMEOUT: (
        "現在的情況：\n"
        "- 玩家在這場戲進行到一半時離開了，之後就沒有再回來。\n"
        "- 請以「你離開之後，她自己…」的角度收：鏡頭留在角色身上，"
        "寫她獨自面對這個沒演完的場面時做了什麼、想了什麼。\n"
        "- 玩家沒有道別、沒有給任何回應——那個空白本身就是這場戲的結局，"
        "把它寫成角色感受到的東西，而不是替玩家補上一句話。"
    ),
}

_VERDICT_BLOCKS = {
    SCENE_CLOSE_RESOLVED: (
        "你要做的判斷：\n"
        "- 若這場戲**還沒**演完，只輸出 resolved 為 false，"
        "closing_narration 與 canon_summary 都給空字串——這是最常見的答案，不要勉強收場。\n"
        "- 只有在戲真的走完了才輸出 resolved 為 true，"
        "並同時寫出收尾旁白與正史摘要。"
    ),
    SCENE_CLOSE_MANUAL: (
        "你要做的事：\n"
        "- 這場戲一定要收，resolved 固定輸出 true。\n"
        "- 同時寫出收尾旁白與正史摘要。"
    ),
    SCENE_CLOSE_TIMEOUT: (
        "你要做的事：\n"
        "- 這場戲一定要收，resolved 固定輸出 true。\n"
        "- 同時寫出收尾旁白與正史摘要。"
    ),
}

_FORCED_MODES = frozenset({SCENE_CLOSE_MANUAL, SCENE_CLOSE_TIMEOUT})


class NullStorySceneCloser(StorySceneCloserPort):
    """Null object for wiring without an LLM: never wraps a scene up.

    A real implementation rather than ``None`` so the service depends on
    the port unconditionally. In a deployment with no model this means
    scenes never end by verdict — the player's 「結束場景」 button and
    SC1-E's idle sweep still close them, just without narration, which is
    the documented degradation rather than a special case.
    """

    async def write_closing(
        self, context: StorySceneClosingContext,
    ) -> StorySceneClosingDraft | None:
        return None


class LLMStorySceneCloser(StorySceneCloserPort):
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

    async def write_closing(
        self, context: StorySceneClosingContext,
    ) -> StorySceneClosingDraft | None:
        # The fake backend emits deterministic junk. On the verdict path
        # that junk could end a live scene, and on the forced paths it
        # would be written into the character's long-term memory as
        # something that happened.
        try:
            if await self._resolver.is_fake(character=context.character):
                return None
            raw = await asyncio.wait_for(
                self._resolver.generate(
                    _build_prompt(context),
                    character=context.character,
                ),
                timeout=_TIMEOUT_SECONDS,
            )
        except Exception:
            _LOGGER.info(
                "story scene closer unavailable character=%s scene=%s mode=%s",
                context.character.id,
                context.session.id,
                context.mode,
                exc_info=True,
            )
            return None
        return _parse_draft(raw, context=context)


def _build_prompt(context: StorySceneClosingContext) -> str:
    session = context.session
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    body = get_default_loader().render(
        "story/scene_closer",
        character_name=context.character.name,
        character_summary=context.character.summary or _UNSPECIFIED,
        speaking_style=context.character.speaking_style or "自然",
        scene_title=session.title or _UNSPECIFIED,
        scene_location=session.location or _UNSPECIFIED,
        scene_mood=session.mood or _UNSPECIFIED,
        scene_type=session.scene_type or _UNSPECIFIED,
        # Layer 3 (side story) has no beat and therefore no dramatic
        # question; the verdict then rests on the transcript alone, which
        # is what the placeholder tells the model.
        dramatic_question=session.dramatic_question or _UNSPECIFIED,
        operator_position_block=render_closer_operator_position_block(
            session.operator_position, session.operator_note,
        ),
        today=context.today.isoformat() if context.today else _UNSPECIFIED,
        date_context_block=(
            render_story_date_context_block(
                context.today,
                language_tag=context.operator_primary_language,
                include_today_line=False,
            )
            if context.today is not None else ""
        ),
        transcript=(
            "\n".join(context.transcript)
            if context.transcript
            else "（這場戲只有開場，之後沒有任何對話）"
        ),
        mode_block=_MODE_BLOCKS.get(
            context.mode, _MODE_BLOCKS[SCENE_CLOSE_MANUAL],
        ),
        verdict_block=_VERDICT_BLOCKS.get(
            context.mode, _VERDICT_BLOCKS[SCENE_CLOSE_MANUAL],
        ),
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _parse_draft(
    raw: str, *, context: StorySceneClosingContext,
) -> StorySceneClosingDraft | None:
    payload = _extract_json_object(raw)
    if payload is None:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    if not _citations_hold(parsed.get("player_actions_referenced"), context):
        # A wrap-up that claims the player did something they never did is
        # worse than no wrap-up: it becomes both thread history and canon
        # memory. Refuse the whole draft — the scene either keeps playing
        # or closes without prose.
        _LOGGER.warning(
            "story scene closer invented a player action; draft refused "
            "character=%s scene=%s mode=%s",
            context.character.id,
            context.session.id,
            context.mode,
        )
        return None

    narration = _clean(parsed.get("closing_narration"), _MAX_NARRATION_CHARS)
    summary = _clean(parsed.get("canon_summary"), _MAX_SUMMARY_CHARS)
    tone = _clean(parsed.get("emotional_tone"), 40) or None

    if context.mode in _FORCED_MODES:
        # Already decided. Anything usable is enough; both halves empty
        # means the model produced nothing to keep.
        if not narration and not summary:
            return None
        return StorySceneClosingDraft(
            resolved=True,
            closing_narration=narration,
            canon_summary=summary,
            emotional_tone=tone,
        )

    # Verdict path. An affirmative verdict with nothing written is not a
    # verdict — the scene keeps playing and the next turn asks again,
    # which costs one short call instead of a scene ending into silence.
    if parsed.get("resolved") is not True or not narration or not summary:
        return StorySceneClosingDraft(resolved=False)
    return StorySceneClosingDraft(
        resolved=True,
        closing_narration=narration,
        canon_summary=summary,
        emotional_tone=tone,
    )


def _citations_hold(
    cited: object, context: StorySceneClosingContext,
) -> bool:
    """Every quoted player action must be traceable to a real player line.

    An absent or empty list passes: the model is claiming the wrap-up
    describes no player action at all, and there is nothing to verify.
    A non-empty list against a player who never spoke fails absolutely.
    """
    if not isinstance(cited, list) or not cited:
        return True
    haystack = tuple(
        _normalize(line) for line in context.player_lines
        if _normalize(line)
    )
    if not haystack:
        return False
    for quote in cited:
        needle = _normalize(_PLAYER_LABEL_RE.sub("", str(quote or "")))
        if not needle:
            continue
        if not any(needle in line for line in haystack):
            return False
    return True


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip()


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
