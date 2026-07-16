"""LLM projection for the player-facing "how she sees you" surface."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Any, TYPE_CHECKING

from kokoro_link.application.dto.operator_persona_projection import (
    PersonaProjectionFactResponse,
    PersonaProjectionResponse,
)
from kokoro_link.application.services.feature_keys import FEATURE_PERSONA_PROJECTION
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.profile_field import ProfileField
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

if TYPE_CHECKING:  # pragma: no cover
    from kokoro_link.application.services.character_service import CharacterService
    from kokoro_link.application.services.operator_persona_service import (
        OperatorPersonaService,
    )
    from kokoro_link.domain.entities.character import Character
    from kokoro_link.domain.entities.operator_persona import OperatorPersona


_LOGGER = logging.getLogger(__name__)

_PROJECTION_ALLOWED_LAYERS = frozenset({1, 2})
_PROJECTION_EXCLUDED_LAYERS = frozenset({3, 5})
_PROJECTION_MIN_CONFIDENCE = {1: 0.7, 2: 0.7}
_CACHE_TTL_SECONDS = 60.0
_MAX_FACT_VALUE_CHARS = 160
_MAX_NARRATIVE_CHARS = 800

_LAYER1_SAFE_LABELS: dict[str, str] = {
    "name": "名字",
    "nickname": "稱呼",
    "age": "年齡",
    "occupation": "工作",
    "company_or_school": "公司 / 學校",
    "residence": "居住地",
}

_LAYER2_SAFE_LABELS: dict[str, str] = {
    "interests": "興趣",
    "diet": "飲食偏好",
    "routine": "日常節奏",
    "consumption_style": "消費習慣",
    "life_goals": "生活目標",
}


class OperatorPersonaProjectionCharacterNotFoundError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _ProjectionFact:
    field_id: str | None
    layer: int
    field_key: str
    label: str
    value: str
    confidence: float
    last_updated: str


@dataclass(frozen=True, slots=True)
class _ProjectionCacheEntry:
    created_at: float
    signature: tuple[tuple[str, str, str, float, str], ...]
    response: PersonaProjectionResponse


class OperatorPersonaProjectionService:
    """Build the player-facing persona narrative through an LLM.

    The raw operator-persona aggregate is a debug/admin mirror. This
    service deliberately projects only a small, low-risk Layer 1/2
    subset into a warm first-person narrative and a minimal correction
    list. Layer 3 and Layer 5 are excluded at the module-constant level
    so they cannot drift into the UI through route wiring.
    """

    def __init__(
        self,
        *,
        character_service: "CharacterService",
        persona_service: "OperatorPersonaService",
        active_llm_provider: ActiveLLMProviderPort,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
        operator_profile_service: Any | None = None,
    ) -> None:
        self._character_service = character_service
        self._persona_service = persona_service
        self._active_llm_provider = active_llm_provider
        self._cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self._cache: dict[tuple[str, str], _ProjectionCacheEntry] = {}
        self._operator_profile_service = operator_profile_service

    async def project(
        self,
        character_id: str,
        *,
        operator_id: str = DEFAULT_OPERATOR_ID,
    ) -> PersonaProjectionResponse:
        character = await self._load_owned_character(character_id, operator_id)
        if character is None:
            raise OperatorPersonaProjectionCharacterNotFoundError(
                "Character not found",
            )
        persona = await self._persona_service.get_current(character.id, operator_id)
        facts = _safe_projection_facts(persona)
        signature = _fact_signature(facts)
        cache_key = (character.id, operator_id)
        cached = self._cache.get(cache_key)
        if cached is not None and self._cache_valid(cached, signature):
            return cached.response

        fact_responses = _fact_responses(facts)
        narrative = ""
        if facts:
            language = await self._resolve_operator_language(operator_id)
            narrative = await self._generate_narrative(
                character, facts, language=language,
            )
        response = PersonaProjectionResponse(
            character_id=character.id,
            narrative=narrative,
            facts=fact_responses,
            empty=not narrative and not fact_responses,
        )
        self._cache[cache_key] = _ProjectionCacheEntry(
            created_at=monotonic(),
            signature=signature,
            response=response,
        )
        return response

    def invalidate(
        self,
        character_id: str,
        operator_id: str = DEFAULT_OPERATOR_ID,
    ) -> None:
        self._cache.pop((character_id, operator_id), None)

    async def _load_owned_character(
        self,
        character_id: str,
        operator_id: str,
    ) -> "Character | None":
        try:
            return await self._character_service.get_character_entity(
                character_id,
                user_id=operator_id,
            )
        except TypeError:
            character = await self._character_service.get_character_entity(
                character_id,
            )
            if (
                character is not None
                and getattr(character, "user_id", DEFAULT_OPERATOR_ID) != operator_id
            ):
                return None
            return character

    def _cache_valid(
        self,
        entry: _ProjectionCacheEntry,
        signature: tuple[tuple[str, str, str, float, str], ...],
    ) -> bool:
        return (
            self._cache_ttl_seconds > 0
            and entry.signature == signature
            and monotonic() - entry.created_at <= self._cache_ttl_seconds
        )

    async def _resolve_operator_language(self, operator_id: str) -> str:
        """Resolve the operator's content language for the narrative half.

        Falls back to the ship-first ``zh-TW`` when no profile service is
        wired (legacy / tests). The fact-label half is language-agnostic
        (D6: stable ``field_key`` translated on the frontend), so only the
        LLM narrative needs this hint."""
        default = "zh-TW"
        if self._operator_profile_service is None:
            return default
        try:
            operator = await self._operator_profile_service.get_for_user(
                operator_id,
            )
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = (getattr(operator, "primary_language", "") or "").strip()
        return lang or default

    async def _generate_narrative(
        self,
        character: "Character",
        facts: tuple[_ProjectionFact, ...],
        *,
        language: str = "zh-TW",
    ) -> str:
        try:
            if await self._active_llm_provider.is_fake(
                FEATURE_PERSONA_PROJECTION,
                character=character,
            ):
                return ""
            model = await self._active_llm_provider.resolve(
                FEATURE_PERSONA_PROJECTION,
                character=character,
            )
            model_id = await self._active_llm_provider.resolve_model_id(
                FEATURE_PERSONA_PROJECTION,
                character=character,
            )
        except Exception:
            _LOGGER.exception("persona projection model resolution failed")
            return ""

        prompt = _build_projection_prompt(character, facts, language=language)
        try:
            raw = await model.generate(prompt, model=model_id)
        except Exception:
            _LOGGER.exception(
                "persona projection LLM call failed character=%s",
                character.id,
            )
            return ""
        return _parse_narrative(raw)


def _safe_projection_facts(persona: "OperatorPersona") -> tuple[_ProjectionFact, ...]:
    facts: list[_ProjectionFact] = []
    for layer, labels, fields in (
        (1, _LAYER1_SAFE_LABELS, persona.layer1_identity),
        (2, _LAYER2_SAFE_LABELS, persona.layer2_life),
    ):
        if layer not in _PROJECTION_ALLOWED_LAYERS:
            continue
        for field_key, label in labels.items():
            fld = fields.get(field_key)
            if fld is None or not _passes_projection_threshold(fld):
                continue
            value = _trim_value(fld.value, _MAX_FACT_VALUE_CHARS)
            if not value:
                continue
            facts.append(
                _ProjectionFact(
                    field_id=fld.field_id,
                    layer=layer,
                    field_key=field_key,
                    label=label,
                    value=value,
                    confidence=fld.confidence,
                    last_updated=fld.last_updated.isoformat(),
                ),
            )
    return tuple(facts)


def _passes_projection_threshold(fld: ProfileField) -> bool:
    if fld.content_mode is MessageContentMode.NSFW:
        return False
    if fld.layer in _PROJECTION_EXCLUDED_LAYERS:
        return False
    if fld.layer not in _PROJECTION_ALLOWED_LAYERS:
        return False
    threshold = _PROJECTION_MIN_CONFIDENCE.get(fld.layer)
    return threshold is not None and fld.confidence >= threshold


def _fact_responses(
    facts: tuple[_ProjectionFact, ...],
) -> list[PersonaProjectionFactResponse]:
    responses: list[PersonaProjectionFactResponse] = []
    for fact in facts:
        if not fact.field_id:
            continue
        responses.append(
            PersonaProjectionFactResponse(
                field_id=fact.field_id,
                field_key=fact.field_key,
                label=fact.label,
                value=fact.value,
            ),
        )
    return responses


def _fact_signature(
    facts: tuple[_ProjectionFact, ...],
) -> tuple[tuple[str, str, str, float, str], ...]:
    return tuple(
        (
            fact.field_id or "",
            fact.field_key,
            fact.value,
            fact.confidence,
            fact.last_updated,
        )
        for fact in facts
    )


def _build_projection_prompt(
    character: "Character",
    facts: tuple[_ProjectionFact, ...],
    *,
    language: str = "zh-TW",
) -> str:
    facts_payload = [
        {
            "layer": fact.layer,
            "field_key": fact.field_key,
            "label": fact.label,
            "value": fact.value,
        }
        for fact in facts
    ]
    language_hint = render_operator_language_hint(language)
    return "\n".join(
        [
            *([language_hint] if language_hint else []),
            "你是角色視角的使用者畫像投影器，不是資料庫瀏覽器。",
            f"角色名字：{character.name}",
            f"角色摘要：{_trim_value(character.summary, 240) or '未設定'}",
            "",
            "任務：根據結構化事實，寫一段角色第一人稱會如何看待玩家的短敘事。",
            "只使用 facts 中明確提供的內容；不得補充、推測或延伸未提供的個資。",
            "不要輸出清單、不要提到 layer / confidence / evidence / database / prompt。",
            "不要碰觸脆弱情緒推論、信任依賴、秘密、金錢、人際關係狀態或家庭細節。",
            "語氣要像她在回憶玩家，而不是客服摘要；1 到 3 句即可。",
            "",
            "facts:",
            json.dumps(facts_payload, ensure_ascii=False, indent=2),
            "",
            "輸出 JSON 物件，不要 markdown、不要 code fence、不要前言：",
            '{"narrative":"第一人稱短敘事"}',
        ],
    )


def _parse_narrative(raw: str) -> str:
    payload = _extract_json_object(raw)
    if payload is None:
        return ""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    return _clean_generated_text(parsed.get("narrative"), max_chars=_MAX_NARRATIVE_CHARS)


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
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


def _clean_generated_text(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.strip().split())
    cleaned = cleaned.strip("「」\"'")
    return _trim_value(cleaned, max_chars)


def _trim_value(value: str | None, max_chars: int) -> str:
    text = " ".join((value or "").strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
