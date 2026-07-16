"""LLM-backed 16 型性格 analyzer."""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.feature_keys import (
    FEATURE_CHARACTER_PERSONALITY_TYPE,
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
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

_LOGGER = logging.getLogger(__name__)
_VALID_CONFLICT_LEVELS = {"none", "soft", "blocking"}
_MAX_NOTES = 5
_MAX_QUESTIONS = 3
_MAX_TEXT_CHARS = 180


class LLMCharacterPersonalityTypeAnalyzer(CharacterPersonalityTypeAnalyzerPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=FEATURE_CHARACTER_PERSONALITY_TYPE,
        )

    async def analyze(
        self,
        request: CharacterPersonalityTypeAnalysisInput,
    ) -> CharacterPersonalityTypeAnalysis:
        fallback = _fallback_analysis(request)
        if await self._resolver.is_fake():
            return fallback
        try:
            raw = await self._resolver.generate(_build_prompt(request))
        except Exception:
            _LOGGER.exception("character personality type analyzer LLM call failed")
            return fallback
        obj = _extract_object(raw)
        if obj is None:
            return fallback
        return _parse_analysis(obj, fallback=fallback)


def _fallback_analysis(
    request: CharacterPersonalityTypeAnalysisInput,
) -> CharacterPersonalityTypeAnalysis:
    selected = request.user_selected_type
    current = request.current_type
    suggested = selected or current or CharacterPersonalityType.DEFAULT  # type: ignore[attr-defined]
    return CharacterPersonalityTypeAnalysis(
        suggested_type=suggested,
        is_consistent=True,
        conflict_level="none",
    )


def _build_prompt(request: CharacterPersonalityTypeAnalysisInput) -> str:
    language_hint = render_operator_language_hint(request.operator_primary_language)
    selected = request.user_selected_type
    current = request.current_type
    lines = [
        *([language_hint, ""] if language_hint else []),
        "你是角色創作一致性分析器。請根據角色設定，建議或檢查 16 型性格。",
        "",
        "角色資料：",
        f"- 名字：{request.name or '（未提供）'}",
        f"- 簡介：{request.summary or '（未提供）'}",
        f"- personality：{', '.join(request.personality) or '（未提供）'}",
        f"- interests：{', '.join(request.interests) or '（未提供）'}",
        f"- speaking_style：{request.speaking_style or '（未提供）'}",
        f"- boundaries：{', '.join(request.boundaries) or '（未提供）'}",
        f"- aspirations：{', '.join(request.aspirations) or '（未提供）'}",
        f"- 使用者手選類型：{selected.code if selected else '（未手選）'}",
        f"- 目前類型：{current.code if current else '（未設定）'}",
        "",
        "判斷原則：",
        "- 16 型只是角色創作參考，不是心理診斷或絕對規則。",
        "- 使用者手選類型時，不要擅自覆蓋；只檢查它和人設是否一致。",
        "- 沒有手選時，可以依人設建議一個類型；信心低就輸出空 code。",
        "- 若類型與具體人設衝突，請用自然語言提醒，讓使用者修人設、修類型，或補合理反差。",
        "- 不要用 J/P、E/I 等軸做僵硬推理；以整體角色設定為準。",
        "",
        "只輸出 JSON 物件，不要 code fence。欄位：",
        "{",
        '  "suggested_code": "ISTJ|...|空字串",',
        '  "confidence": 0.0-1.0,',
        '  "source": "llm_inferred|user_explicit|unset",',
        '  "is_consistent": true,',
        '  "conflict_level": "none|soft|blocking",',
        '  "rationale": "短句",',
        '  "conflict_notes": ["短句"],',
        '  "user_questions": ["最多 3 題自然問題"]',
        "}",
    ]
    return "\n".join(lines)


def _parse_analysis(
    obj: dict[str, Any],
    *,
    fallback: CharacterPersonalityTypeAnalysis,
) -> CharacterPersonalityTypeAnalysis:
    code = _coerce_str(obj.get("suggested_code"), 8).upper()
    source = _coerce_str(obj.get("source"), 24) or "unset"
    if not code:
        source = "unset"
    try:
        suggested = CharacterPersonalityType(
            code=code,
            source=source,
            confidence=_coerce_float(obj.get("confidence")),
            rationale=_coerce_str(obj.get("rationale"), _MAX_TEXT_CHARS),
            consistency_notes=tuple(
                _coerce_str_list(obj.get("conflict_notes"), limit=_MAX_NOTES),
            ),
        )
    except ValueError:
        suggested = fallback.suggested_type

    conflict_level = _coerce_str(obj.get("conflict_level"), 24).lower()
    if conflict_level not in _VALID_CONFLICT_LEVELS:
        conflict_level = "none"
    return CharacterPersonalityTypeAnalysis(
        suggested_type=suggested,
        is_consistent=bool(obj.get("is_consistent", conflict_level == "none")),
        conflict_level=conflict_level,
        conflict_notes=tuple(
            _coerce_str_list(obj.get("conflict_notes"), limit=_MAX_NOTES),
        ),
        user_questions=tuple(
            _coerce_str_list(obj.get("user_questions"), limit=_MAX_QUESTIONS),
        ),
    )


def _extract_object(text: str) -> dict[str, Any] | None:
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
                try:
                    parsed = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _coerce_str(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        return value.strip()[:max_chars]
    return ""


def _coerce_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _coerce_str_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _coerce_str(item, _MAX_TEXT_CHARS)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out
