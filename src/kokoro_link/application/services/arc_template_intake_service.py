"""Arc-template authoring wizard (Phase 2.7 of SCENE_BEAT_PLAN).

The frontend wizard drives a multi-step modal that, at each step, can
ask this service for **LLM-suggested options** so the operator only
needs to pick a chip rather than type. There's also a one-shot
``generate_full_draft`` for the "approve everything" fast path.

The service is stateless — every method takes the partial template
state from the caller and returns suggestions or a refined fragment.
The wizard accumulates state on the frontend; only the final
``save_template`` call hits disk.

Failure semantics:

- LLM call fails → return safe fallback values (empty list of
  suggestions, ``None`` for single fields). The wizard surfaces this
  as "AI 沒給建議，自己打字 OK" rather than a hard error.
- ``save_template`` is the one method that does raise, because
  silently dropping the operator's authored work would be much worse
  than a visible failure.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from kokoro_link.application.services.feature_keys import (
    FEATURE_ARC_TEMPLATE_INTAKE,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_SCOPE_GENERIC,
    DEFAULT_TONE,
    ArcTemplate,
    ArcTemplateBeat,
    ArcTemplateBinding,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


# ---------- DTOs (plain dataclasses, REST layer adapts to Pydantic) ----


@dataclass(frozen=True, slots=True)
class MetaSuggestions:
    titles: list[str]
    """Up to 3 suggested Chinese titles based on the operator's pitch."""
    themes: list[str]
    """Up to 3 themes — common values: ambition / friendship / loss /
    discovery / transformation / redemption / custom."""
    tones: list[str]
    """Up to 3 tones — daily / dramatic / mature / dark / lighthearted."""
    world_frames: list[str]
    """Suggested character.world_frame values fitting the pitch."""


@dataclass(frozen=True, slots=True)
class BeatOptions:
    """Per-beat suggestions for a single position in the arc."""

    titles: list[str]
    locations: list[str]
    scene_characters: list[str]
    """Suggested NPC labels (operator picks any subset, also can add)."""
    dramatic_questions: list[str]
    scene_types: list[str]
    """Subset of {encounter, revelation, conflict, resolution,
    interlude} that fits the position; UI can highlight the first as
    "recommended"."""


@dataclass(frozen=True, slots=True)
class BeatDraft:
    """Skeleton of a single beat the wizard hands the service to fill
    in. Mirrors the persistence shape of ``ArcTemplateBeat`` but each
    field is optional so partially-filled drafts can ask for help."""

    sequence: int
    day_offset: int
    title: str = ""
    summary: str = ""
    tension: str = "rising"
    scene_type: str = "encounter"
    location: str | None = None
    scene_characters: tuple[str, ...] = ()
    dramatic_question: str | None = None
    required: bool = True


@dataclass(frozen=True, slots=True)
class TemplateDraft:
    """Full draft passed to ``save_template`` after the wizard wraps."""

    id: str
    title: str
    premise: str
    theme: str
    language: str = ""
    """BCP-47-ish language tag of the authored prose. Empty = undeclared;
    ``save_template`` falls back to the operator's stored primary
    language at save time (see ``_draft_to_template``)."""
    tone: str = DEFAULT_TONE
    duration_days: int = 14
    world_frames: tuple[str, ...] = ()
    required_traits: tuple[str, ...] = ()
    applicability_scope: str = ARC_TEMPLATE_SCOPE_GENERIC
    target_character_ids: tuple[str, ...] = ()
    beats: tuple[BeatDraft, ...] = ()


def template_draft_from_llm_json(data: dict[str, Any]) -> TemplateDraft | None:
    """Parse an LLM JSON object into the shared review-draft shape."""

    return _build_full_draft_from_json(data)


def extract_llm_json(raw: str) -> Any:
    """Parse the first JSON object/array from tolerant LLM text output."""

    return _extract_json_object(raw)


@dataclass(frozen=True, slots=True)
class BeatContext:
    """Caller-supplied context for ``suggest_beat_options``.

    The service reads this to know what the operator has already
    committed to so suggestions stay coherent.
    """

    template_title: str
    premise: str
    theme: str
    tone: str
    duration_days: int
    world_frames: tuple[str, ...]
    beat_position: int
    """0-based position of this beat in the beats list."""
    total_beats: int
    """Total number of main-line beats the operator chose."""
    day_offset: int
    """Already-decided day_offset for this beat (from the rhythm
    pattern Stage 3 chose)."""
    tension: str
    """Already-decided tension for this beat (also from rhythm
    pattern). UI can still let the operator override."""
    prior_titles: tuple[str, ...] = ()
    """Titles of previously-confirmed beats so suggestions don't
    repeat them."""


# ---------- Service ----------


class ArcTemplateIntakeService:
    def __init__(
        self,
        *,
        repository: ArcTemplateRepositoryPort,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
    ) -> None:
        self._repository = repository
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=FEATURE_ARC_TEMPLATE_INTAKE,
        )

    # ----- Stage 1: meta -----

    async def suggest_meta(
        self, pitch: str, *, operator_primary_language: str = "zh-TW",
    ) -> MetaSuggestions:
        """Given a one-line operator pitch, propose title / theme /
        tone / world_frame candidates.

        Pitch examples: "想寫一個內向角色準備鋼琴比賽的故事" /
        "黑暗奇幻戰爭劇" / "兩個人緩慢分手"
        """
        empty = MetaSuggestions(
            titles=[], themes=[], tones=[], world_frames=[],
        )
        if not pitch.strip():
            return empty
        if await self._resolver.is_fake():
            return _meta_fallback(pitch)
        prompt = _build_meta_prompt(pitch, operator_primary_language)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("intake suggest_meta LLM call failed")
            return _meta_fallback(pitch)
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            return _meta_fallback(pitch)
        return MetaSuggestions(
            titles=_coerce_str_list(data.get("titles"), limit=3, max_len=20),
            themes=_coerce_str_list(data.get("themes"), limit=3, max_len=24),
            tones=_coerce_str_list(data.get("tones"), limit=3, max_len=24),
            world_frames=_coerce_str_list(
                data.get("world_frames"), limit=4, max_len=20,
            ),
        )

    # ----- Stage 2: premise -----

    async def condense_premise(
        self,
        *,
        logline: str,
        start_state: str,
        end_state: str,
        tone: str = DEFAULT_TONE,
        operator_primary_language: str = "zh-TW",
    ) -> str:
        """Compress operator's three short answers into a 60–120 char
        premise paragraph. Returns the original ``logline`` on failure
        so the wizard never blocks on AI hiccups."""
        if not logline.strip():
            return ""
        if await self._resolver.is_fake():
            return _premise_fallback(logline, start_state, end_state)
        prompt = _build_premise_prompt(
            logline=logline, start_state=start_state,
            end_state=end_state, tone=tone,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("intake condense_premise LLM call failed")
            return _premise_fallback(logline, start_state, end_state)
        # Premise is plain text — no JSON envelope to unwrap.
        cleaned = _strip_fences(raw).strip()
        if not cleaned:
            return _premise_fallback(logline, start_state, end_state)
        # Cap at ~150 chars so a runaway LLM doesn't blow past the
        # prompt-block budget.
        if len(cleaned) > 200:
            cleaned = cleaned[:200].rstrip() + "…"
        return cleaned

    # ----- Stage 4: per-beat -----

    async def suggest_beat_options(
        self, context: BeatContext, *, operator_primary_language: str = "zh-TW",
    ) -> BeatOptions:
        """Propose 3–4 candidates per field for a single beat.

        Wizard renders these as chips; operator clicks one or types
        free text. The service is stateless — caller passes the full
        context every call.
        """
        empty = BeatOptions(
            titles=[], locations=[], scene_characters=[],
            dramatic_questions=[], scene_types=[],
        )
        if await self._resolver.is_fake():
            return _beat_options_fallback(context)
        prompt = _build_beat_options_prompt(context, operator_primary_language)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("intake suggest_beat_options LLM call failed")
            return _beat_options_fallback(context)
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            return _beat_options_fallback(context)
        return BeatOptions(
            titles=_coerce_str_list(data.get("titles"), limit=4, max_len=24),
            locations=_coerce_str_list(
                data.get("locations"), limit=4, max_len=20,
            ),
            scene_characters=_coerce_str_list(
                data.get("scene_characters"), limit=5, max_len=20,
            ),
            dramatic_questions=_coerce_str_list(
                data.get("dramatic_questions"), limit=4, max_len=60,
            ),
            scene_types=_coerce_str_list(
                data.get("scene_types"), limit=3, max_len=16,
            ),
        )

    async def generate_beat_summary(
        self,
        *,
        beat: BeatDraft,
        context: BeatContext,
        operator_primary_language: str = "zh-TW",
    ) -> str:
        """Write a 100–150 char summary for a single beat from the
        skeleton fields. The summary is what eventually feeds the
        runtime expander, so the prose register matters."""
        if await self._resolver.is_fake():
            return _beat_summary_fallback(beat)
        prompt = _build_beat_summary_prompt(
            beat=beat, context=context,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("intake generate_beat_summary LLM call failed")
            return _beat_summary_fallback(beat)
        cleaned = _strip_fences(raw).strip()
        if not cleaned:
            return _beat_summary_fallback(beat)
        if len(cleaned) > 250:
            cleaned = cleaned[:250].rstrip() + "…"
        return cleaned

    # ----- One-shot fast path -----

    async def generate_full_draft(
        self,
        *,
        pitch: str,
        hint: str = "",
        operator_primary_language: str = "zh-TW",
    ) -> TemplateDraft | None:
        """Produce a complete template draft from minimal input.

        Operator clicks "全部交給 AI"; the service runs one LLM call
        that emits the whole template. Returns ``None`` on failure so
        the wizard can fall back to step-by-step authoring instead of
        leaving the operator stranded.
        """
        if not pitch.strip():
            return None
        if await self._resolver.is_fake():
            return None
        prompt = _build_full_draft_prompt(
            pitch=pitch, hint=hint,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("intake generate_full_draft LLM call failed")
            return None
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            return None
        return _build_full_draft_from_json(data)

    # ----- Save -----

    async def save_template(
        self,
        draft: TemplateDraft,
        *,
        user_id: str,
        overwrite: bool = False,
        operator_language: str = "",
    ) -> str:
        """Persist a finished draft as a user-authored template.

        Translates the wizard-flavoured ``TemplateDraft`` (loose
        validation) into a strict ``ArcTemplate`` and writes it through
        the repository's per-user save. ``user_id`` is the owner from
        the request's auth context; pack rows (``user_id IS NULL``)
        are unreachable from this surface. Validation errors propagate
        so the wizard can show "id already taken" / "title required".

        ``operator_language`` is the caller's stored primary language,
        used as a fallback when the draft itself doesn't declare one
        (the common case: the wizard never asked). This is pure
        metadata passthrough, not structural behaviour — see
        ``ArcTemplate.language`` docstring.
        """
        template = _draft_to_template(draft, fallback_language=operator_language)
        return await self._repository.save_for_user(
            template, user_id=user_id, overwrite=overwrite,
        )


# ---------- Prompt builders ----------


def _build_meta_prompt(pitch: str, operator_primary_language: str = "zh-TW") -> str:
    language_hint = render_operator_language_hint(operator_primary_language)
    language_line = f"{language_hint}\n" if language_hint else ""
    return (
        f"{language_line}"
        "你是 Yuralume 的劇情骨架編輯。"
        "依下方使用者一句話 pitch，提案範本的標題／主題／調性／適用世界觀。\n\n"
        f"使用者 pitch：{pitch.strip()}\n\n"
        "輸出規則：\n"
        "- 只輸出一個 JSON 物件，不要任何前言、不要 code fence。\n"
        "- 形狀：{\"titles\": [str, str, str], \"themes\": [str, str, str],\n"
        "  \"tones\": [str, str, str], \"world_frames\": [str, ...]}\n"
        "- titles：3 個簡短標題（約 8–14 個全形字或等寬長度，用玩家語言），"
        "各從不同切入角度提案。\n"
        "- themes：3 個從 ambition / friendship / loss / discovery / "
        "transformation / redemption / custom 中挑出最契合的。\n"
        "- tones：3 個從 daily / dramatic / mature / dark / lighthearted "
        "中挑出最契合的。\n"
        "- world_frames：1–4 個從 modern / fantasy / school / custom 中"
        "挑（不確定就空陣列）。\n"
    )


def _build_premise_prompt(
    *,
    logline: str,
    start_state: str,
    end_state: str,
    tone: str,
    operator_primary_language: str = "zh-TW",
) -> str:
    language_hint = render_operator_language_hint(operator_primary_language)
    language_line = f"{language_hint}\n" if language_hint else ""
    return (
        f"{language_line}"
        "你是 Yuralume 的劇情骨架編輯。"
        "把下方三段答案濃縮成一段 60–120 字的 premise，第三人稱、有畫面感。\n\n"
        f"整體調性：{tone}\n"
        f"一句話 logline：{logline.strip()}\n"
        f"角色起點：{start_state.strip() or '（未提供）'}\n"
        f"角色終點：{end_state.strip() or '（未提供）'}\n\n"
        "輸出規則：\n"
        "- 只輸出 premise 純文字，不要 JSON、不要編號、不要前言。\n"
        "- 60–120 字，第三人稱。\n"
        "- 不要寫成大綱／時間表，要寫成「這幾週她正在經歷什麼」的氛圍。\n"
        "- 不要在 premise 裡逐字引用使用者的三段答案，要重新表達。\n"
    )


def _build_beat_options_prompt(
    context: BeatContext, operator_primary_language: str = "zh-TW",
) -> str:
    prior = (
        "、".join(context.prior_titles)
        if context.prior_titles else "（這是第一個 beat）"
    )
    frames = ", ".join(context.world_frames) or "未指定"
    language_hint = render_operator_language_hint(operator_primary_language)
    language_line = f"{language_hint}\n" if language_hint else ""
    return (
        f"{language_line}"
        "你是 Yuralume 的劇情骨架編輯。"
        "為下方範本的第 N 個主線 beat 提案 title / location / "
        "scene_characters / dramatic_question / scene_type 候選。\n\n"
        f"範本標題：{context.template_title}\n"
        f"premise：{context.premise}\n"
        f"theme：{context.theme}\n"
        f"tone：{context.tone}\n"
        f"world_frames：{frames}\n"
        f"持續天數：{context.duration_days}\n"
        f"這個 beat 在第 {context.beat_position + 1} / {context.total_beats} 個位置，"
        f"day_offset={context.day_offset}，tension={context.tension}\n"
        f"前面已確定的 beat 標題：{prior}\n\n"
        "輸出規則：\n"
        "- 只輸出一個 JSON 物件，不要任何前言、不要 code fence。\n"
        "- 形狀：{\"titles\": [...4], \"locations\": [...4], "
        "\"scene_characters\": [...5], \"dramatic_questions\": [...4], "
        "\"scene_types\": [...3]}\n"
        "- titles：4 個短句（約 4–10 個全形字或等寬長度，用玩家語言），"
        "不要與前面 beat 重複。\n"
        "- locations：4 個適合該 world_frame + tone 的場景地點短語。\n"
        "- scene_characters：5 個出場 NPC 名字提案（不含主角，可隨意取名）。"
        "如果這位置適合獨白，第一個放空字串 \"\"。\n"
        "- dramatic_questions：4 個一句問句，「這場戲在解什麼？」。\n"
        "- scene_types：從 encounter / revelation / conflict / resolution / "
        "interlude 中依該 tension 挑出 3 個最合適的，把最推薦的放第一個。\n"
    )


def _build_beat_summary_prompt(
    *,
    beat: BeatDraft,
    context: BeatContext,
    operator_primary_language: str = "zh-TW",
) -> str:
    npcs = (
        "、".join(beat.scene_characters)
        if beat.scene_characters else "（獨白）"
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    language_line = f"{language_hint}\n" if language_hint else ""
    return (
        f"{language_line}"
        "你是 Yuralume 的劇情骨架編輯。"
        "依下方 beat 結構寫一段 100–150 字的 summary，"
        "供 runtime 的 expander 將來「演出」這場戲。\n\n"
        f"範本 tone：{context.tone}（影響語氣，不要與此衝突）\n"
        f"範本 premise：{context.premise}\n"
        f"beat 標題：{beat.title}\n"
        f"day_offset：{beat.day_offset}（在 {context.duration_days} 天 arc 中）\n"
        f"tension：{beat.tension}\n"
        f"scene_type：{beat.scene_type}\n"
        f"location：{beat.location or '未指定'}\n"
        f"scene_characters：{npcs}\n"
        f"dramatic_question：{beat.dramatic_question or '未指定'}\n\n"
        "輸出規則：\n"
        "- 100–150 字，第三人稱，純文字（不要 JSON、不要編號）。\n"
        "- 寫成「這場戲的氛圍與發生的核心動作」，不要寫成條列式大綱。\n"
        "- 包含：場景在哪、誰在做什麼、角色感受到什麼、戲劇問題如何浮現。\n"
        "- 維持範本的整體 tone（daily 不要塞戰爭場面，mature 不要過度收斂）。\n"
        "- 不要把使用者寫進場景。\n"
    )


def _build_full_draft_prompt(
    *, pitch: str, hint: str, operator_primary_language: str = "zh-TW",
) -> str:
    hint_line = (
        f"使用者額外說明：{hint.strip()}\n"
        if hint and hint.strip() else ""
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    language_line = f"{language_hint}\n" if language_hint else ""
    return (
        f"{language_line}"
        "你是 Yuralume 的劇情骨架編輯。"
        "從下方 pitch 一口氣產出一份完整的 arc template。\n\n"
        f"使用者 pitch：{pitch.strip()}\n"
        f"{hint_line}"
        "\n"
        "輸出規則：\n"
        "- 只輸出一個 JSON 物件，不要任何前言、不要 code fence。\n"
        "- 形狀：{\n"
        "    \"id\": str (snake_case 英文短句，當檔名),\n"
        "    \"title\": str (約 8–14 個全形字或等寬長度，用玩家語言),\n"
        "    \"premise\": str (60–120 字，第三人稱),\n"
        "    \"theme\": str (ambition/friendship/loss/discovery/transformation/redemption/custom),\n"
        "    \"tone\": str (daily/dramatic/mature/dark/lighthearted),\n"
        "    \"duration_days\": int (7–30),\n"
        "    \"world_frames\": [str, ...] (modern/fantasy/school/custom 中挑),\n"
        "    \"required_traits\": [],\n"
        "    \"beats\": [\n"
        "      {\n"
        "        \"sequence\": int, \"day_offset\": int,\n"
        "        \"title\": str, \"summary\": str (100–150 字),\n"
        "        \"tension\": str (setup/rising/climax/falling/resolution),\n"
        "        \"scene_type\": str (encounter/revelation/conflict/resolution/interlude),\n"
        "        \"location\": str (可空字串),\n"
        "        \"scene_characters\": [str, ...] (可空陣列),\n"
        "        \"dramatic_question\": str (可空字串),\n"
        "        \"required\": bool\n"
        "      }, ...\n"
        "    ]\n"
        "  }\n"
        "- beats 數量 5–8，依經典三幕分布 day_offset。\n"
        "- 至少 60% 的 beats 標 required=true。\n"
    )


# ---------- Fallbacks (when LLM unavailable / fake provider) ----------


def _meta_fallback(pitch: str) -> MetaSuggestions:
    """Static fallback so the wizard works in fake-provider / offline
    mode. Operator can still type custom answers."""
    return MetaSuggestions(
        titles=[pitch.strip()[:14]] if pitch.strip() else [],
        themes=["custom"],
        tones=[DEFAULT_TONE],
        world_frames=[],
    )


def _premise_fallback(logline: str, start: str, end: str) -> str:
    parts = [s.strip() for s in (logline, start, end) if s and s.strip()]
    return " ".join(parts) or logline.strip()


def _beat_options_fallback(context: BeatContext) -> BeatOptions:
    return BeatOptions(
        titles=[],
        locations=[],
        scene_characters=[],
        dramatic_questions=[],
        scene_types=[context.tension],  # at least the auto-derived one
    )


def _beat_summary_fallback(beat: BeatDraft) -> str:
    bits: list[str] = []
    if beat.location:
        bits.append(f"在{beat.location}")
    if beat.scene_characters:
        bits.append(f"與{'、'.join(beat.scene_characters)}")
    if beat.title:
        bits.append(f"發生「{beat.title}」")
    if beat.dramatic_question:
        bits.append(f"——{beat.dramatic_question}")
    return "，".join(bits) or beat.title


# ---------- JSON / coercion helpers ----------


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text or "").replace("```", "")


def _extract_json_object(raw: str) -> Any:
    """Locate the outermost JSON object / array in ``raw`` and parse it.

    Returns the parsed value or ``None`` on any failure. Tolerant of
    leading prose, trailing commentary, and code fences — matches the
    pattern used by other LLM-output adapters in the codebase.
    """
    text = _strip_fences(raw or "").strip()
    if not text:
        return None
    # Prefer object form; fall back to array form.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start < 0 or end <= start:
            continue
        blob = text[start: end + 1]
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            continue
    return None


def _coerce_str_list(
    raw: Any, *, limit: int, max_len: int = 80,
) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip()
        # Empty string is sometimes meaningful (signals "leave blank")
        # — keep one if it's the first entry the LLM emitted.
        if cleaned == "" and not seen:
            out.append("")
            seen.add("")
            continue
        if not cleaned or cleaned in seen:
            continue
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len]
        out.append(cleaned)
        seen.add(cleaned)
        if len(out) >= limit:
            break
    return out


# ---------- Draft → ArcTemplate conversion ----------


def _draft_to_template(
    draft: TemplateDraft, *, fallback_language: str = "",
) -> ArcTemplate:
    if not draft.beats:
        raise ValueError("TemplateDraft.beats must be non-empty")
    binding = ArcTemplateBinding(
        world_frames=tuple(draft.world_frames),
        required_traits=tuple(draft.required_traits),
    )
    beats = [
        ArcTemplateBeat.create(
            sequence=b.sequence,
            day_offset=b.day_offset,
            title=b.title,
            summary=b.summary,
            tension=b.tension,
            scene_type=b.scene_type,
            location=b.location,
            scene_characters=b.scene_characters,
            dramatic_question=b.dramatic_question,
            required=b.required,
        )
        for b in draft.beats
    ]
    return ArcTemplate.create(
        id=draft.id,
        title=draft.title,
        premise=draft.premise,
        theme=draft.theme,
        language=(draft.language or fallback_language),
        tone=draft.tone,
        duration_days=draft.duration_days,
        beats=beats,
        binding=binding,
        applicability_scope=draft.applicability_scope,
        target_character_ids=draft.target_character_ids,
    )


def _build_full_draft_from_json(data: dict[str, Any]) -> TemplateDraft | None:
    raw_beats = data.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        return None
    beats: list[BeatDraft] = []
    for index, raw in enumerate(raw_beats):
        if not isinstance(raw, dict):
            continue
        title = (raw.get("title") or "").strip()
        summary = (raw.get("summary") or "").strip()
        if not title or not summary:
            continue
        beats.append(
            BeatDraft(
                sequence=_coerce_int(raw.get("sequence"), default=index),
                day_offset=_coerce_int(raw.get("day_offset"), default=0),
                title=title,
                summary=summary,
                tension=(raw.get("tension") or "rising").strip().lower(),
                scene_type=(raw.get("scene_type") or "encounter").strip().lower(),
                location=_optional_str(raw.get("location")),
                scene_characters=tuple(
                    _coerce_str_list(
                        raw.get("scene_characters"), limit=6, max_len=24,
                    ),
                ),
                dramatic_question=_optional_str(raw.get("dramatic_question")),
                required=bool(raw.get("required", True)),
            )
        )
    if not beats:
        return None
    title = (data.get("title") or "").strip()
    premise = (data.get("premise") or "").strip()
    if not title or not premise:
        return None
    return TemplateDraft(
        id=(data.get("id") or "").strip() or _slug_from_title(title),
        title=title,
        premise=premise,
        theme=(data.get("theme") or "custom").strip(),
        tone=(data.get("tone") or DEFAULT_TONE).strip() or DEFAULT_TONE,
        duration_days=_coerce_int(data.get("duration_days"), default=14),
        world_frames=tuple(
            _coerce_str_list(data.get("world_frames"), limit=4, max_len=20),
        ),
        required_traits=tuple(
            _coerce_str_list(data.get("required_traits"), limit=6, max_len=20),
        ),
        applicability_scope=(
            data.get("applicability_scope")
            if isinstance(data.get("applicability_scope"), str)
            else ARC_TEMPLATE_SCOPE_GENERIC
        ),
        target_character_ids=tuple(
            _coerce_str_list(
                data.get("target_character_ids"), limit=12, max_len=64,
            ),
        ),
        beats=tuple(beats),
    )


def _coerce_int(raw: Any, *, default: int) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return default
    return default


def _optional_str(raw: Any) -> str | None:
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or None
    return None


def _slug_from_title(title: str) -> str:
    """Last-resort id when the LLM forgets to provide one. Strips
    non-ASCII and falls back to ``arc_template_<n chars>`` so the
    repository ``save`` still has a usable filename stem."""
    ascii_only = re.sub(r"[^a-z0-9_]+", "_", title.lower())
    cleaned = ascii_only.strip("_")
    return cleaned[:40] or f"arc_template_{abs(hash(title)) % 100000}"
