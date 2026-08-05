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
from dataclasses import dataclass, field, replace
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
from kokoro_link.domain.entities.story_arc import normalise_operator_position
from kokoro_link.domain.services.story_tone_policy import (
    filter_suggested_tones,
    fold_stored_tone,
    resolve_prompt_tone,
    tone_vocabulary,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
# Mirrors the planner ingest path's bound (OP1-A,
# ``llm_arc_planner._MAX_OPERATOR_NOTE_CHARS``) — every other optional
# string this file's LLM-response parsers accept is length-clamped;
# ``operator_note`` wasn't (Codex review, bonus finding alongside M1/M2).
_MAX_OPERATOR_NOTE_CHARS = 120


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
    # --- Player's place in this scene (OP0 / OP1-B) -------------------
    # ``None`` = unjudged. ``generate_beat_summary`` below proposes a
    # value (``BeatSummarySuggestion``); the wizard pre-fills it here
    # and the operator can still overwrite or clear it back to unjudged.
    operator_position: str | None = None
    operator_note: str | None = None


@dataclass(frozen=True, slots=True)
class BeatSummarySuggestion:
    """Result of ``generate_beat_summary`` (OP1-B).

    The same LLM call that writes the beat's prose now also proposes
    where the operator (player) stands in the scene — the wizard's old
    hard rule ("don't write the operator into the scene") is retired in
    favour of asking the model to judge honestly. ``operator_position``/
    ``operator_note`` are a *suggestion* the wizard pre-fills into the
    beat draft; the operator can still edit or clear it back to
    unjudged. Both default to ``None`` (unjudged) so a model response
    that only manages to produce a summary (the pre-OP1-B contract, or
    a fallback) never fabricates a position.

    Codex review fix (M1): when the incoming ``beat`` already carries a
    decided ``operator_position``/``operator_note``, ``generate_beat_summary``
    anchors this suggestion back onto that existing decision before
    returning it (see ``_anchor_operator_decision``) — regenerating the
    summary must write *around* an operator's call, never renegotiate
    it out from under them.
    """

    summary: str
    operator_position: str | None = None
    operator_note: str | None = None


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
        cloud_mode: bool = False,
    ) -> None:
        self._repository = repository
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=FEATURE_ARC_TEMPLATE_INTAKE,
        )
        # GF6 — hosted deployments must not author ``mature`` templates:
        # the tone vocabulary the prompts name drops it, a model that
        # proposes it anyway gets filtered, and a draft that still
        # carries it is folded on save. Default ``False`` keeps the
        # self-host wizard byte-identical.
        self._cloud_mode = cloud_mode

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
        prompt = _build_meta_prompt(
            pitch, operator_primary_language, cloud_mode=self._cloud_mode,
        )
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
            # GF6: the prompt already omits blocked tones from the
            # vocabulary; a model that proposes one anyway must not put
            # it in front of a hosted operator.
            tones=filter_suggested_tones(
                _coerce_str_list(data.get("tones"), limit=3, max_len=24),
                cloud_mode=self._cloud_mode,
            ),
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
            cloud_mode=self._cloud_mode,
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
        prompt = _build_beat_options_prompt(
            context, operator_primary_language, cloud_mode=self._cloud_mode,
        )
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
    ) -> BeatSummarySuggestion:
        """Write a 100–150 char summary for a single beat from the
        skeleton fields, plus a proposal for the operator's place in
        the scene (OP1-B). The summary is what eventually feeds the
        runtime expander, so the prose register matters; the position
        proposal is a suggestion the wizard pre-fills — never a save.

        Every return path is anchored (``_anchor_operator_decision``)
        against ``beat``'s own already-decided position/note before
        going back to the caller — a crash, a stale-format response, or
        a model that simply judges differently on rerun must never
        silently revert an operator's call (Codex review fix, M1)."""
        if await self._resolver.is_fake():
            return _anchor_operator_decision(_beat_summary_fallback(beat), beat)
        prompt = _build_beat_summary_prompt(
            beat=beat, context=context,
            operator_primary_language=operator_primary_language,
            cloud_mode=self._cloud_mode,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("intake generate_beat_summary LLM call failed")
            return _anchor_operator_decision(_beat_summary_fallback(beat), beat)
        return _anchor_operator_decision(
            _parse_beat_summary_response(raw, beat), beat,
        )

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
            cloud_mode=self._cloud_mode,
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
        # GF6: hosted, a draft that still carries a blocked tone (a
        # hand-crafted request, or an edit of a legacy row) is corrected
        # rather than refused — the wizard echoes the saved row back, so
        # the substitution is visible and the operator is never locked
        # out of editing their own template.
        draft = replace(
            draft,
            tone=fold_stored_tone(
                draft.tone,
                cloud_mode=self._cloud_mode,
                context="arc template wizard save",
            ),
        )
        template = _draft_to_template(draft, fallback_language=operator_language)
        return await self._repository.save_for_user(
            template, user_id=user_id, overwrite=overwrite,
        )


# ---------- Prompt builders ----------


def _build_meta_prompt(
    pitch: str,
    operator_primary_language: str = "zh-TW",
    *,
    cloud_mode: bool = False,
) -> str:
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
        # GF6: hosted drops ``mature`` from the vocabulary the model is
        # allowed to pick from; self-host keeps the full catalogue.
        f"- tones：3 個從 {tone_vocabulary(cloud_mode=cloud_mode)} "
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
    cloud_mode: bool = False,
) -> str:
    language_hint = render_operator_language_hint(operator_primary_language)
    language_line = f"{language_hint}\n" if language_hint else ""
    # GF6: the wizard's in-progress tone is operator input that lands
    # verbatim in a prompt, so hosted it goes through the same policy
    # the runtime scene writers use.
    tone = resolve_prompt_tone(
        tone, cloud_mode=cloud_mode, context="intake premise",
    )
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
    context: BeatContext,
    operator_primary_language: str = "zh-TW",
    *,
    cloud_mode: bool = False,
) -> str:
    tone = resolve_prompt_tone(
        context.tone, cloud_mode=cloud_mode, context="intake beat options",
    )
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
        f"tone：{tone}\n"
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
    cloud_mode: bool = False,
) -> str:
    tone = resolve_prompt_tone(
        context.tone, cloud_mode=cloud_mode, context="intake beat summary",
    )
    npcs = (
        "、".join(beat.scene_characters)
        if beat.scene_characters else "（獨白）"
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    language_line = f"{language_hint}\n" if language_hint else ""
    # Codex review fix (M1): a beat the operator has already judged
    # must not be re-opened by a "regenerate summary" click. When
    # ``beat.operator_position`` is already decided, tell the model
    # this is a fixed fact to write the summary *around* — not a
    # question to answer again — instead of the open three-way ask
    # below. ``_anchor_operator_decision`` is the enforcement backstop
    # for a model that ignores this and answers differently anyway.
    if beat.operator_position is not None:
        note_clause = f"（{beat.operator_note}）" if beat.operator_note else ""
        position_instruction = (
            f"使用者在這場戲的位置已經確定為 {beat.operator_position}"
            f"{note_clause}，這是既定事實、不是待你判斷的問題——"
            "請把 summary 寫成與這個位置一致的版本。"
            "JSON 裡的 operator_position／operator_note 請直接照抄這個"
            "既定值，不要另外判斷或改寫。\n\n"
        )
        schema_position_literal = f"\"{beat.operator_position}\""
    else:
        position_instruction = (
            "使用者的位置請依劇情誠實判斷，分三種：\n"
            "- absent：使用者不在這場戲（角色獨自行動，或只與上述 NPC 互動）。\n"
            "- present：使用者在場，但這場戲的重心不是使用者"
            "（例如使用者陪伴、旁觀，戲劇問題仍是角色自己的）。\n"
            "- central：這場戲就是關於使用者——沒有使用者這場戲演不下去"
            "（例如角色向使用者告白、求助、對峙、坦白）。\n"
            "不要因為習慣把使用者排除在外就一律選 absent："
            "若 dramatic_question 本來就指向角色與使用者的關係，"
            "應誠實標為 present 或 central。\n"
            "operator_note 選填，一句話描述使用者在這場戲的戲劇位置"
            "（例如「她要向你坦白」）；沒有明確立場就給空字串。\n\n"
        )
        schema_position_literal = "\"absent\"|\"present\"|\"central\""
    return (
        f"{language_line}"
        "你是 Yuralume 的劇情骨架編輯。"
        "依下方 beat 結構寫一段 100–150 字的 summary，"
        "供 runtime 的 expander 將來「演出」這場戲；"
        "同時提案使用者（玩家）在這場戲裡的位置。\n\n"
        f"範本 tone：{tone}（影響語氣，不要與此衝突）\n"
        f"範本 premise：{context.premise}\n"
        f"beat 標題：{beat.title}\n"
        f"day_offset：{beat.day_offset}（在 {context.duration_days} 天 arc 中）\n"
        f"tension：{beat.tension}\n"
        f"scene_type：{beat.scene_type}\n"
        f"location：{beat.location or '未指定'}\n"
        f"scene_characters（其他登場角色，不含使用者）：{npcs}\n"
        f"dramatic_question：{beat.dramatic_question or '未指定'}\n\n"
        f"{position_instruction}"
        "輸出規則：\n"
        "- 只輸出一個 JSON 物件，不要任何前言、不要 code fence。\n"
        "- 形狀：{\"summary\": str, "
        f"\"operator_position\": {schema_position_literal}, "
        "\"operator_note\": str}\n"
        "- summary：100–150 字，第三人稱，純文字，"
        "寫成「這場戲的氛圍與發生的核心動作」，不要寫成條列式大綱。\n"
        "- summary 包含：場景在哪、誰在做什麼、角色感受到什麼、"
        "戲劇問題如何浮現。\n"
        # GF6: the "heavy register" example names ``mature`` self-host;
        # hosted it names whatever the policy folds ``mature`` into.
        f"- 維持範本的整體 tone（daily 不要塞戰爭場面，"
        f"{resolve_prompt_tone('mature', cloud_mode=cloud_mode)}"
        " 不要過度收斂）。\n"
    )


def _build_full_draft_prompt(
    *,
    pitch: str,
    hint: str,
    operator_primary_language: str = "zh-TW",
    cloud_mode: bool = False,
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
        # Codex review fix (M2): the one-shot "全部交給 AI" fast path
        # used to never ask for a player position at all — every beat
        # it produced landed unjudged even though the parse side
        # (OP0-B) has carried the two columns since day one. Mirrors
        # the planner's guidance (arc_planner.txt, OP1-A) so the two
        # producer paths teach the model the same vocabulary and the
        # same LLM-first mixed-cadence instruction (no fixed ratio).
        "每個 beat 也要提案使用者（玩家）在這場戲裡的位置，用 "
        "operator_position 這個獨立欄位表達，三選一：\n"
        "- absent：玩家不在這場戲裡。角色自己、或角色與 scene_characters "
        "就能把這場戲演完。\n"
        "- present：玩家在場，但這場戲不是關於他——他在旁邊、陪著、看著、"
        "被牽動。\n"
        "- central：這場戲是關於玩家的，少了他就演不成"
        "（攤牌、告白、決裂、和解、只想說給他聽的那種話）。\n"
        "張力最高的節點（tension 是 climax 或 resolution，以及任何攤牌／"
        "告白／決裂／和解性質的戲）傾向讓玩家在場、甚至成為核心——這是一段"
        "有玩家在裡面的關係故事；日常推進、角色自己的工作與內心整理、純"
        "鋪陳性質的 beat，讓角色一個人（或跟 scene_characters）走完是好"
        "的。不要按固定比例分配，也不要每顆 beat 都填同一個值，依這條 arc "
        "的劇情本身逐顆判斷。\n"
        "operator_note 是選填的一句話，寫玩家在這場戲裡的戲劇位置"
        "（例如「她要在這裡對你說出那件事」）；absent 的 beat 通常不需要，"
        "用不到就填空字串。\n\n"
        "輸出規則：\n"
        "- 只輸出一個 JSON 物件，不要任何前言、不要 code fence。\n"
        "- 形狀：{\n"
        "    \"id\": str (snake_case 英文短句，當檔名),\n"
        "    \"title\": str (約 8–14 個全形字或等寬長度，用玩家語言),\n"
        "    \"premise\": str (60–120 字，第三人稱),\n"
        "    \"theme\": str (ambition/friendship/loss/discovery/transformation/redemption/custom),\n"
        # GF6: hosted, the one-shot path must not be told ``mature``
        # is on the menu either.
        "    \"tone\": str ("
        f"{tone_vocabulary(cloud_mode=cloud_mode, separator='/')}),\n"
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
        "        \"required\": bool,\n"
        "        \"operator_position\": str (absent/present/central 之一),\n"
        "        \"operator_note\": str (可空字串)\n"
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


def _beat_summary_fallback(beat: BeatDraft) -> BeatSummarySuggestion:
    """Static fallback (fake provider / LLM crash). Stitches the
    structured fields into a usable sentence like ``_meta_fallback``'s
    siblings — but it has no semantic judgement to offer, so the
    player-position proposal stays unjudged (``None``) rather than
    guessing. That judgement call is exactly what OP1-B hands to the
    LLM; a rule-based heuristic must not fake it (LLM-first)."""
    bits: list[str] = []
    if beat.location:
        bits.append(f"在{beat.location}")
    if beat.scene_characters:
        bits.append(f"與{'、'.join(beat.scene_characters)}")
    if beat.title:
        bits.append(f"發生「{beat.title}」")
    if beat.dramatic_question:
        bits.append(f"——{beat.dramatic_question}")
    summary = "，".join(bits) or beat.title
    return BeatSummarySuggestion(summary=summary)


def _anchor_operator_decision(
    suggestion: BeatSummarySuggestion, beat: BeatDraft,
) -> BeatSummarySuggestion:
    """Never let a regenerated summary silently reopen a player-position
    call the operator already made (Codex review fix, M1).

    Gated per field, independently: a beat whose ``operator_position``
    is already decided keeps that exact value no matter what this call
    produced — crash fallback, off-vocabulary hallucination, or a
    model that simply judges differently on rerun; a beat whose
    ``operator_note`` already carries text keeps that note. Only a
    still-unjudged field (``None`` / blank) accepts this call's fresh
    proposal. ``_build_beat_summary_prompt`` already tells the model
    about an existing decision so the *summary prose* it writes stays
    consistent with it — this function is the enforcement backstop for
    when the model doesn't comply (or the call fails outright).
    """
    position = (
        beat.operator_position
        if beat.operator_position is not None
        else suggestion.operator_position
    )
    note = (
        beat.operator_note
        if (beat.operator_note or "").strip()
        else suggestion.operator_note
    )
    if position == suggestion.operator_position and note == suggestion.operator_note:
        return suggestion
    return BeatSummarySuggestion(
        summary=suggestion.summary,
        operator_position=position,
        operator_note=note,
    )


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
            operator_position=b.operator_position,
            operator_note=b.operator_note,
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
                # The full-draft prompt now asks for both columns too
                # (M2, Codex review — it used to only ask per-beat via
                # ``_build_beat_summary_prompt``). Parsed defensively
                # regardless: a garbage value degrades to unjudged
                # rather than failing the whole draft; it's a
                # review-step suggestion, not a save.
                operator_position=_decode_operator_position(
                    raw.get("operator_position"),
                ),
                operator_note=_optional_str(
                    raw.get("operator_note"), max_len=_MAX_OPERATOR_NOTE_CHARS,
                ),
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


def _optional_str(raw: Any, *, max_len: int | None = None) -> str | None:
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return None
        return cleaned[:max_len] if max_len is not None else cleaned
    return None


def _decode_operator_position(raw: Any) -> str | None:
    """Best-effort read of an LLM-suggested position value.

    Same forgiving contract as the persistence-layer twins
    (``sa_arc_template_repository._decode_operator_position`` /
    ``sa_story_arc_repository._decode_operator_position``): a value the
    domain would reject degrades to ``None`` (unjudged) rather than
    failing the whole draft — this is a review-step suggestion the
    operator can still edit, not a persisted row.
    """
    try:
        return normalise_operator_position(raw)
    except ValueError:
        _LOGGER.warning(
            "arc template intake: LLM operator_position %r is not a "
            "known position — treating as unjudged",
            raw,
        )
        return None


def _clip_summary(text: str) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) > 250:
        cleaned = cleaned[:250].rstrip() + "…"
    return cleaned


def _parse_beat_summary_response(
    raw: str, beat: BeatDraft,
) -> BeatSummarySuggestion:
    """Decode ``generate_beat_summary``'s LLM response (OP1-B).

    Prefers the JSON envelope (summary + player-position proposal). A
    model that ignores the envelope and returns plain prose degrades to
    treating the whole response as the summary text — the pre-OP1-B
    contract — rather than discarding a usable answer; the position
    proposal is simply absent for that call (stays unjudged, same as
    every other degradation path in this file).
    """
    data = _extract_json_object(raw)
    if isinstance(data, dict) and isinstance(data.get("summary"), str):
        summary = _clip_summary(data["summary"])
        if summary:
            return BeatSummarySuggestion(
                summary=summary,
                operator_position=_decode_operator_position(
                    data.get("operator_position"),
                ),
                operator_note=_optional_str(
                    data.get("operator_note"), max_len=_MAX_OPERATOR_NOTE_CHARS,
                ),
            )
    cleaned = _clip_summary(_strip_fences(raw).strip())
    if not cleaned:
        return _beat_summary_fallback(beat)
    return BeatSummarySuggestion(summary=cleaned)


def _slug_from_title(title: str) -> str:
    """Last-resort id when the LLM forgets to provide one. Strips
    non-ASCII and falls back to ``arc_template_<n chars>`` so the
    repository ``save`` still has a usable filename stem."""
    ascii_only = re.sub(r"[^a-z0-9_]+", "_", title.lower())
    cleaned = ascii_only.strip("_")
    return cleaned[:40] or f"arc_template_{abs(hash(title)) % 100000}"
