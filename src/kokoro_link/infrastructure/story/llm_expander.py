"""LLM-backed implementation of ``StoryEventExpanderPort``.

Takes a one-line seed + the character's voice/context and returns a
2–3 sentence narrative in first person plus an optional emotional
tone tag. Parsing is forgiving — we keep the expander best-effort so
a bad LLM day doesn't crash the whole gacha cycle.
"""

from __future__ import annotations

import json
import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story import SceneContext, StoryEventExpanderPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_MAX_NARRATIVE_CHARS = 240
_ALLOWED_TONES = {
    "peaceful", "happy", "melancholy", "lonely", "curious",
    "excited", "anxious", "nostalgic", "tired", "content",
    "restless", "hopeful",
}


class NullStoryEventExpander(StoryEventExpanderPort):
    """Fallback expander when no real LLM provider is wired.

    Uses the seed text verbatim wrapped in minimal first-person framing
    so the pipeline still produces plausible output in fake-provider
    mode and in unit tests.
    """

    async def expand(
        self,
        *,
        seed: StorySeed,
        character_name: str,
        character_summary: str,
        speaking_style: str,
        world_frame: str,
        scene: SceneContext | None = None,
        character: Character | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[str, str | None]:
        # Scene context is intentionally ignored in the null path —
        # we already have a static-template fallback and adding a
        # second template just to handle scenes adds noise. The
        # downstream prompt block still reads the structured fields
        # from the beat itself.
        del scene, character  # reserved for symmetry with the LLM expander
        return (f"今天{seed.seed_text}。", None)


class LLMStoryEventExpander(StoryEventExpanderPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def expand(
        self,
        *,
        seed: StorySeed,
        character_name: str,
        character_summary: str,
        speaking_style: str,
        world_frame: str,
        scene: SceneContext | None = None,
        character: Character | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[str, str | None]:
        # Single fallback closure — every error path drops to the
        # null expander with the same arguments. Inlining the call
        # six times made the function hard to keep in sync (we
        # actually missed ``scene`` plumbing twice during review).
        async def _fallback() -> tuple[str, str | None]:
            return await NullStoryEventExpander().expand(
                seed=seed,
                character_name=character_name,
                character_summary=character_summary,
                speaking_style=speaking_style,
                world_frame=world_frame,
                scene=scene,
                operator_primary_language=operator_primary_language,
            )

        if await self._resolver.is_fake(character=character):
            return await _fallback()
        prompt = _build_prompt(
            seed=seed,
            character_name=character_name,
            character_summary=character_summary,
            speaking_style=speaking_style,
            world_frame=world_frame,
            scene=scene,
            character=character,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception("story expander LLM call failed")
            # Fall through to null — we prefer a plain narrative over an
            # aborted gacha cycle. The event still fires; it just reads
            # a little flat.
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

        narrative = _clean_narrative(parsed.get("narrative"))
        tone = _normalise_tone(parsed.get("tone"))
        if not narrative:
            return await _fallback()
        return (narrative, tone)


def _build_prompt(
    *,
    seed: StorySeed,
    character_name: str,
    character_summary: str,
    speaking_style: str,
    world_frame: str,
    scene: SceneContext | None,
    character: Character | None = None,
    operator_primary_language: str = "zh-TW",
) -> str:
    if scene is not None and scene.is_meaningful():
        return _build_scene_prompt(
            seed=seed,
            character_name=character_name,
            character_summary=character_summary,
            speaking_style=speaking_style,
            world_frame=world_frame,
            scene=scene,
            character=character,
            operator_primary_language=operator_primary_language,
        )
    # Tags only exist on real StorySeed objects; arc beats wrapped
    # via ``_BeatAsSeed`` don't carry them, so we tolerate the absence.
    tags = getattr(seed, "tags", ())
    body = get_default_loader().render(
        "story/expander_seed",
        character_name=character_name,
        character_summary=character_summary or "（未設定）",
        identity_block=_identity_block(character),
        speaking_style=speaking_style or "自然",
        world_frame=world_frame,
        seed_text=seed.seed_text,
        tags="、".join(tags) if tags else "（無）",
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


_SCENE_TYPE_HINTS: dict[str, str] = {
    "encounter": "日常／相遇場——細節與當下的感受比衝突重要",
    "revelation": "頓悟／揭露場——重點是角色想通了什麼，內心的轉變",
    "conflict": "衝突／拉扯場——角色與他人或自己的張力，要寫出對立與選擇",
    "resolution": "解決／釋懷場——把先前的張力收束，給一個明確的落點",
    "interlude": "過場／喘息場——節奏放慢，留白多於事件",
}


# Tone-specific framing lines + style hints. Inserted into the scene
# prompt's task section so the same scene structure can read as gentle
# slice-of-life or grim drama depending on the arc's tone.
#
# Each entry is (header, style_lines). The header replaces the generic
# "把這場戲的「進入時刻」演出來" framing; style_lines are appended to
# the bullet list of constraints.
_TONE_PROFILES: dict[str, tuple[str, list[str]]] = {
    "daily": (
        "用第一人稱寫 3–5 句話，把這場戲的「進入時刻」演出來：",
        [],  # baseline — no extra constraints
    ),
    "dramatic": (
        "用第一人稱寫 3–5 句話，把這場戲的張力推到讀者能感覺到的程度："
        "讓內在的拉扯具體化（呼吸、目光、停頓），而不是用形容詞堆積。",
        [
            "- 情緒可以強烈但不要瑪麗蘇／中二化；用具體動作與細節傳遞重量。",
            "- 戲劇問題的張力要明確浮上來，不要用「終於」「命運」這類抽象詞偷渡。",
        ],
    ),
    "mature": (
        "用第一人稱寫 3–5 句話演出這場戲，**不要迴避**戲劇的真實重量——"
        "暴力、肉體、權力支配、酒精、性、創傷的細節都可以據實寫，"
        "服務於場景而非為刺激而刺激。",
        [
            "- 不要用童書語言粉飾殘酷或親密；該寫實的就寫實。",
            "- 描寫身體感官（疼痛、體溫、氣味、味道）讓場景有重量，但不要獵奇。",
            "- 角色面對創傷時的反應不必體面，可以抗拒、麻木、解離、報復性的清醒。",
        ],
    ),
    "dark": (
        "用第一人稱寫 3–5 句話進入這場戲，重心放在心理層次的不適與曖昧——"
        "讓讀者感受到角色內在某個東西鬆動、扭曲、或無聲地崩塌。",
        [
            "- 避免直接說「我很痛苦」「我崩潰了」；用感官錯位（聲音變遠、時間變慢）"
            "或不合常理的小動作（重複按一個按鈕、盯著一個無關的東西）來呈現。",
            "- 道德可以模糊、答案可以缺席；不必收束成正面的領悟。",
            "- 不要用比喻或詩意語言迴避真相，越平靜的句子越能托出黑暗。",
        ],
    ),
    "lighthearted": (
        "用第一人稱寫 3–5 句話演出這場戲，"
        "在角色當下的尷尬／笑點／吐槽裡帶出戲劇問題——",
        [
            "- 可以有自我吐槽、誇張的內心 OS、輕喜劇式的小反差，"
            "但不要瑪麗蘇／賣萌。",
            "- 即使是衝突場，也用幽默的視角化解重量；戲劇問題仍然要在場，"
            "只是用比較輕的姿態浮現。",
        ],
    ),
}


def _build_scene_prompt(
    *,
    seed: StorySeed,
    character_name: str,
    character_summary: str,
    speaking_style: str,
    world_frame: str,
    scene: SceneContext,
    character: Character | None = None,
    operator_primary_language: str = "zh-TW",
) -> str:
    """Prompt the expander to *play* a scripted beat instead of
    paraphrasing a seed."""
    scene_hint = _SCENE_TYPE_HINTS.get(scene.scene_type, _SCENE_TYPE_HINTS["encounter"])
    location_line = (
        f"- 場景地點：{scene.location}"
        if scene.location
        else "- 場景地點：未指定（請挑一個與角色生活相符的地方）"
    )
    npc_line = (
        f"- 出場人物（除了角色自己）：{'、'.join(scene.scene_characters)}"
        if scene.scene_characters
        else "- 出場人物：只有角色自己（內心戲 / 獨白）"
    )
    question_line = (
        f"- 戲劇問題：{scene.dramatic_question}"
        if scene.dramatic_question
        else "- 戲劇問題：（未指定，可由場景自然引出）"
    )
    required_line = (
        "- 重要性：這是主線必演場景，請完整鋪陳開場 + 情緒落點。"
        if scene.required
        else "- 重要性：這是輔助場景，可以寫得克制、留白多一點。"
    )
    # Tone profile picks the framing line + extra style constraints.
    # Unknown tones fall through to "daily" so a wizard-authored
    # template using a brand-new tone label still works (just reads
    # as default daily framing until we add a profile for it).
    tone_header, tone_extras = _TONE_PROFILES.get(
        scene.tone, _TONE_PROFILES["daily"],
    )
    base_constraints = [
        "- 場景在哪裡、出場人物在做什麼、角色感受到什麼。",
        "- 若有戲劇問題，要讓問題的張力浮現，但不必在此 beat 立刻解答。",
        "- 維持角色的說話風格與世界觀，不要塞入與所在世界不符的物件。",
        "- 不要把使用者寫進這場戲；這是角色自己正在經歷的瞬間。",
        "- 不要把骨架文字逐句照抄，要用角色語氣重寫。",
        "- 總長不超過 160 字。",
    ]
    constraint_block = "\n".join(base_constraints + tone_extras)
    body = get_default_loader().render(
        "story/expander_scene",
        character_name=character_name,
        character_summary=character_summary or "（未設定）",
        identity_block=_identity_block(character),
        speaking_style=speaking_style or "自然",
        world_frame=world_frame,
        tone=scene.tone,
        scene_type=scene.scene_type,
        scene_hint=scene_hint,
        location_line=location_line,
        npc_line=npc_line,
        question_line=question_line,
        required_line=required_line,
        seed_text=seed.seed_text,
        tone_header=tone_header,
        constraint_block=constraint_block,
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


def _identity_block(character: Character | None) -> str:
    if character is None:
        return "\n".join(
            [
                "- 性別身份：（未設定；不要從名字、簡介或外觀推斷）",
                "- 第三人稱代稱：（未設定；需要第三人稱稱呼時優先使用角色名或中立表述）",
                "- 視覺性別呈現：（未設定；視覺描述以外觀欄為準，不要由代稱推斷畫面）",
            ]
        )
    return "\n".join(render_character_identity_lines(character))


def _clean_narrative(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > _MAX_NARRATIVE_CHARS:
        text = text[:_MAX_NARRATIVE_CHARS].rstrip() + "…"
    return text


def _normalise_tone(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip().lower()
    if not trimmed or trimmed == "null":
        return None
    if trimmed not in _ALLOWED_TONES:
        # Unknown tone string — drop it rather than propagate garbage.
        return None
    return trimmed


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring, or ``None``."""
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
                return text[start : index + 1]
    return None
