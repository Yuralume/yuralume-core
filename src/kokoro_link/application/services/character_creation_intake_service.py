"""Stateless creation-intake analyzer for initial relationship setup.

The frontend owns wizard state and calls this service after each step.
The service returns either a create-ready verdict or a small set of
natural follow-up questions. LLM failures degrade to a conservative
field-presence fallback so character creation is not blocked by provider
instability.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from kokoro_link.application.dto.character import (
    InitialRelationshipPayload,
    InitialRelationshipSafeUserProfilePayload,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_CHARACTER_CREATION_INTAKE,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.character_personality_type import (
    CharacterPersonalityTypeAnalysis,
    CharacterPersonalityTypeAnalysisInput,
    CharacterPersonalityTypeAnalyzerPort,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
_MAX_QUESTIONS = 3
_MAX_WARNINGS = 3


@dataclass(frozen=True, slots=True)
class CharacterCreationDraftContext:
    name: str = ""
    summary: str = ""
    personality: tuple[str, ...] = ()
    interests: tuple[str, ...] = ()
    speaking_style: str = ""
    boundaries: tuple[str, ...] = ()
    aspirations: tuple[str, ...] = ()
    personality_type_code: str = ""
    personality_type_rationale: str = ""


@dataclass(frozen=True, slots=True)
class IntakeQuestion:
    field: str
    question: str
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IntakeWarning:
    kind: str
    message: str
    blocking: bool = False


@dataclass(frozen=True, slots=True)
class IntakeAnalysis:
    can_create: bool
    missing_required: tuple[str, ...] = ()
    questions: tuple[IntakeQuestion, ...] = ()
    normalized_relationship: InitialRelationshipPayload = field(
        default_factory=InitialRelationshipPayload,
    )
    normalized_user_profile: InitialRelationshipSafeUserProfilePayload = field(
        default_factory=InitialRelationshipSafeUserProfilePayload,
    )
    warnings: tuple[IntakeWarning, ...] = ()


class CharacterCreationIntakeService:
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        personality_type_analyzer: CharacterPersonalityTypeAnalyzerPort | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=FEATURE_CHARACTER_CREATION_INTAKE,
        )
        self._personality_type_analyzer = personality_type_analyzer

    async def analyze(
        self,
        *,
        draft: CharacterCreationDraftContext,
        relationship: InitialRelationshipPayload | None = None,
        current_locale: str = "",
        round_index: int = 0,
    ) -> IntakeAnalysis:
        rel = relationship or InitialRelationshipPayload()
        personality_analysis = await self._analyze_personality_type(
            draft=draft,
            current_locale=current_locale,
        )
        if await self._resolver.is_fake():
            return _merge_personality_analysis(
                _fallback_analysis(rel, current_locale),
                personality_analysis,
                current_locale,
            )
        prompt = _build_prompt(
            draft=draft,
            relationship=rel,
            current_locale=current_locale,
            round_index=round_index,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("character creation intake LLM call failed")
            return _merge_personality_analysis(
                _fallback_analysis(rel, current_locale),
                personality_analysis,
                current_locale,
            )
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            return _merge_personality_analysis(
                _fallback_analysis(rel, current_locale),
                personality_analysis,
                current_locale,
            )
        return _merge_personality_analysis(
            _analysis_from_json(data, rel),
            personality_analysis,
            current_locale,
        )

    async def _analyze_personality_type(
        self,
        *,
        draft: CharacterCreationDraftContext,
        current_locale: str,
    ) -> CharacterPersonalityTypeAnalysis | None:
        analyzer = self._personality_type_analyzer
        if analyzer is None:
            return None
        selected_type = _personality_type_from_draft(draft)
        try:
            return await analyzer.analyze(
                CharacterPersonalityTypeAnalysisInput(
                    name=draft.name,
                    summary=draft.summary,
                    personality=draft.personality,
                    interests=draft.interests,
                    speaking_style=draft.speaking_style,
                    boundaries=draft.boundaries,
                    aspirations=draft.aspirations,
                    user_selected_type=selected_type,
                    current_type=selected_type,
                    operator_primary_language=current_locale or "zh-TW",
                ),
            )
        except Exception:
            _LOGGER.exception("character personality type analysis failed")
            return None


def _analysis_from_json(
    data: dict[str, Any],
    fallback_relationship: InitialRelationshipPayload,
) -> IntakeAnalysis:
    questions = tuple(_question_from_json(item) for item in _coerce_list(data.get("questions"))[:_MAX_QUESTIONS])
    questions = tuple(item for item in questions if item is not None)
    missing = tuple(_coerce_str_list(data.get("missing_required"), limit=_MAX_QUESTIONS))
    warnings = tuple(_warning_from_json(item) for item in _coerce_list(data.get("warnings"))[:_MAX_WARNINGS])
    warnings = tuple(item for item in warnings if item is not None)
    relationship = _relationship_from_json(
        data.get("normalized_relationship"),
        fallback_relationship,
    )
    profile = _profile_from_json(data.get("normalized_user_profile"))
    has_blocking_warning = any(item.blocking for item in warnings)
    can_create = (
        bool(data.get("can_create"))
        and not questions
        and not missing
        and not has_blocking_warning
    )
    return IntakeAnalysis(
        can_create=can_create,
        missing_required=missing,
        questions=questions,
        normalized_relationship=relationship,
        normalized_user_profile=profile,
        warnings=warnings,
    )


def _merge_personality_analysis(
    analysis: IntakeAnalysis,
    personality_analysis: CharacterPersonalityTypeAnalysis | None,
    current_locale: str = "",
) -> IntakeAnalysis:
    if personality_analysis is None:
        return analysis
    warnings = list(analysis.warnings)
    questions = list(analysis.questions)
    missing = list(analysis.missing_required)
    if (
        personality_analysis.conflict_level != "none"
        or personality_analysis.conflict_notes
    ):
        message = "；".join(personality_analysis.conflict_notes).strip()
        if not message:
            message = localized_fallback_text(
                "intake.warning.personality_type_conflict", current_locale,
            )
        warnings.append(IntakeWarning(
            kind="personality_type_conflict",
            message=message,
            blocking=personality_analysis.is_blocking,
        ))
    for question in personality_analysis.user_questions[:_MAX_QUESTIONS]:
        text = _clean_text(question, max_len=180)
        if not text:
            continue
        questions.append(IntakeQuestion(
            field="personality_type",
            question=text,
            suggestions=(),
        ))
    if personality_analysis.is_blocking and "personality_type" not in missing:
        missing.append("personality_type")
    limited_questions = tuple(questions[:_MAX_QUESTIONS])
    limited_missing = tuple(missing[:_MAX_QUESTIONS])
    limited_warnings = tuple(warnings[:_MAX_WARNINGS])
    return IntakeAnalysis(
        can_create=(
            analysis.can_create
            and not limited_questions
            and not limited_missing
            and not any(item.blocking for item in limited_warnings)
        ),
        missing_required=limited_missing,
        questions=limited_questions,
        normalized_relationship=analysis.normalized_relationship,
        normalized_user_profile=analysis.normalized_user_profile,
        warnings=limited_warnings,
    )


def _personality_type_from_draft(
    draft: CharacterCreationDraftContext,
) -> CharacterPersonalityType | None:
    code = (draft.personality_type_code or "").strip()
    if not code:
        return None
    try:
        return CharacterPersonalityType(
            code=code,
            source="user_explicit",
            rationale=draft.personality_type_rationale,
        )
    except ValueError:
        return None


def _question_from_json(item: object) -> IntakeQuestion | None:
    if not isinstance(item, dict):
        return None
    question = _clean_text(item.get("question"), max_len=180)
    if not question:
        return None
    field = _clean_text(item.get("field"), max_len=64) or "relationship"
    return IntakeQuestion(
        field=field,
        question=question,
        suggestions=tuple(_coerce_str_list(item.get("suggestions"), limit=4, max_len=40)),
    )


def _warning_from_json(item: object) -> IntakeWarning | None:
    if not isinstance(item, dict):
        return None
    message = _clean_text(item.get("message"), max_len=180)
    if not message:
        return None
    kind = _clean_text(item.get("kind"), max_len=64) or "note"
    return IntakeWarning(kind=kind, message=message, blocking=bool(item.get("blocking")))


def _relationship_from_json(
    value: object,
    fallback: InitialRelationshipPayload,
) -> InitialRelationshipPayload:
    if not isinstance(value, dict):
        return fallback
    merged = fallback.model_dump()
    field_limits = {
        "living_arrangement": 240,
    }
    for key in (
        "relationship_label",
        "known_context",
        "living_arrangement",
        "user_address_name",
        "character_address_name",
        "tone_distance",
        "familiarity_boundary",
        "schedule_involvement_policy",
        "proactive_cadence_hint",
        "user_profile_notes",
    ):
        if key in value:
            merged[key] = _clean_text(
                value.get(key),
                max_len=field_limits.get(key, 500),
            )
    if "proactive_permission" in value:
        merged["proactive_permission"] = bool(value.get("proactive_permission"))
    merged["confirmed_by_user"] = bool(value.get("confirmed_by_user", fallback.confirmed_by_user))
    return InitialRelationshipPayload.model_validate(merged)


def _profile_from_json(value: object) -> InitialRelationshipSafeUserProfilePayload:
    if not isinstance(value, dict):
        return InitialRelationshipSafeUserProfilePayload()
    return InitialRelationshipSafeUserProfilePayload(
        name=_clean_text(value.get("name"), max_len=80),
        nickname=_clean_text(value.get("nickname"), max_len=80),
        occupation=_clean_text(value.get("occupation"), max_len=120),
        company_or_school=_clean_text(value.get("company_or_school"), max_len=120),
        interests=_coerce_str_list(value.get("interests"), limit=12, max_len=60),
        routine=_clean_text(value.get("routine"), max_len=180),
        life_goals=_coerce_str_list(value.get("life_goals"), limit=8, max_len=80),
    )


def _fallback_analysis(
    relationship: InitialRelationshipPayload,
    current_locale: str = "",
) -> IntakeAnalysis:
    def _q(key: str) -> str:
        return localized_fallback_text(f"intake.q.{key}", current_locale)

    def _s(*keys: str) -> tuple[str, ...]:
        return tuple(
            localized_fallback_text(f"intake.s.{key}", current_locale)
            for key in keys
        )

    missing: list[str] = []
    questions: list[IntakeQuestion] = []
    if _has_relationship_intent(relationship) and not relationship.known_context.strip():
        missing.append("known_context")
        questions.append(IntakeQuestion(
            field="known_context",
            question=_q("known_context"),
            suggestions=_s(
                "known_context.first_meeting",
                "known_context.already_known",
            ),
        ))
    if _has_relationship_intent(relationship) and not relationship.living_arrangement.strip():
        missing.append("living_arrangement")
        questions.append(IntakeQuestion(
            field="living_arrangement",
            question=_q("living_arrangement"),
            suggestions=_s(
                "living_arrangement.together",
                "living_arrangement.nearby",
                "living_arrangement.apart",
            ),
        ))
    if relationship.proactive_permission and not relationship.proactive_cadence_hint.strip():
        missing.append("proactive_cadence_hint")
        questions.append(IntakeQuestion(
            field="proactive_cadence_hint",
            question=_q("proactive_cadence_hint"),
            suggestions=_s(
                "proactive_cadence_hint.once_a_day",
                "proactive_cadence_hint.only_important",
                "proactive_cadence_hint.wait_for_me",
            ),
        ))
    if (
        relationship.schedule_involvement_policy != "none"
        and not relationship.familiarity_boundary.strip()
    ):
        missing.append("familiarity_boundary")
        questions.append(IntakeQuestion(
            field="familiarity_boundary",
            question=_q("familiarity_boundary"),
            suggestions=_s(
                "familiarity_boundary.topics_only",
                "familiarity_boundary.invite_not_assume",
            ),
        ))
    capped_questions = tuple(questions[:_MAX_QUESTIONS])
    capped_missing = tuple(missing[:_MAX_QUESTIONS])
    return IntakeAnalysis(
        can_create=not capped_questions,
        missing_required=capped_missing,
        questions=capped_questions,
        normalized_relationship=relationship,
        normalized_user_profile=relationship.safe_user_profile,
        warnings=(),
    )


def _has_relationship_intent(relationship: InitialRelationshipPayload) -> bool:
    return any((
        relationship.relationship_label.strip(),
        relationship.user_address_name.strip(),
        relationship.character_address_name.strip(),
        relationship.living_arrangement.strip(),
        relationship.tone_distance.strip(),
        relationship.familiarity_boundary.strip(),
        relationship.user_profile_notes.strip(),
        relationship.schedule_involvement_policy != "none",
        relationship.proactive_permission,
        relationship.safe_user_profile.has_values(),
    ))


def _build_prompt(
    *,
    draft: CharacterCreationDraftContext,
    relationship: InitialRelationshipPayload,
    current_locale: str,
    round_index: int,
) -> str:
    payload = {
        "character_draft": {
            "name": draft.name,
            "summary": draft.summary,
            "personality": list(draft.personality),
            "interests": list(draft.interests),
            "speaking_style": draft.speaking_style,
            "boundaries": list(draft.boundaries),
            "aspirations": list(draft.aspirations),
            "personality_type_code": draft.personality_type_code,
            "personality_type_rationale": draft.personality_type_rationale,
        },
        "relationship": relationship.model_dump(),
        "current_locale": current_locale,
        "round_index": round_index,
    }
    language_hint = render_operator_language_hint(current_locale)
    language_line = f"{language_hint}\n" if language_hint else ""
    return (
        f"{language_line}"
        "你是角色創建前的關係與使用者畫像缺口檢查器。\n"
        "任務：只根據使用者明確提供的內容，判斷是否還需要補問，避免角色創好後幻想共同回憶、裝熟、或主動打擾。\n"
        "規則：\n"
        "- 不要要求敏感資料，例如收入、創傷、秘密、家庭細節、精確住址或真實姓名。\n"
        "- 如果使用者設定既有關係但沒有說可知道的背景，要問自然問題。\n"
        "- 如果關係語意暗示共同生活（例如寵物、貼身精靈、家人、室友、同居），但沒有說明住在一起還是分開住，要自然反問居住安排；語意判斷交給你，不要只照字面標籤。\n"
        "- 如果允許創角後主動找使用者，必須有頻率或時機限制。\n"
        "- 如果使用者能出現在行程裡，要確認不要跨過的界線。\n"
        "- 每輪最多 3 題；問題要像創作協助，不像表單錯誤。\n"
        "- normalized_* 只能整理明確內容，不得補完未提供的過去事件。\n"
        "只回傳 JSON object，不要 markdown。\n"
        "JSON schema:\n"
        "{\n"
        '  "can_create": boolean,\n'
        '  "missing_required": ["known_context"],\n'
        '  "questions": [{"field": "known_context", "question": "...", "suggestions": ["..."]}],\n'
        '  "normalized_relationship": {},\n'
        '  "normalized_user_profile": {},\n'
        '  "warnings": [{"kind": "personality_type_conflict", "message": "...", "blocking": false}]\n'
        "}\n"
        f"輸入：{json.dumps(payload, ensure_ascii=False)}"
    )


def _extract_json_object(raw: str) -> Any:
    text = _strip_fences(raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).replace("```", "")


def _coerce_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _coerce_str_list(value: object, *, limit: int, max_len: int = 120) -> list[str]:
    out: list[str] = []
    for item in _coerce_list(value):
        cleaned = _clean_text(item, max_len=max_len)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _clean_text(value: object, *, max_len: int) -> str:
    if value is None:
        return ""
    cleaned = str(value).strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len].rstrip()
    return cleaned
