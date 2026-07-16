"""Build LLM-ready context for conversational persona discovery."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.persona_curiosity import (
    PersonaCuriosityAttemptSummary,
    PersonaCuriosityContext,
    PersonaCuriosityPlan,
    PersonaCuriosityRepositoryPort,
)
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.persona_curiosity import (
    PERSONA_CURIOSITY_STATUS_PLANNED,
    PERSONA_CURIOSITY_SURFACES,
    PersonaCuriosityAttempt,
)
from kokoro_link.domain.value_objects.profile_field import ProfileField


_LAYER1_LABELS: dict[str, str] = {
    "name": "姓名",
    "nickname": "稱呼偏好",
    "age": "年齡",
    "occupation": "工作或身份",
    "company_or_school": "公司或學校",
    "residence": "居住地",
}

_LAYER2_LABELS: dict[str, str] = {
    "interests": "興趣",
    "diet": "飲食偏好",
    "routine": "日常節奏",
    "consumption_style": "消費或內容偏好",
    "life_goals": "近期目標",
}

_SAFE_GAP_GROUPS: tuple[tuple[str, tuple[tuple[int, str], ...]], ...] = (
    ("稱呼或暱稱", ((1, "name"), (1, "nickname"))),
    ("工作、學校或生活場域", ((1, "occupation"), (1, "company_or_school"))),
    ("興趣、充電方式或常聊話題", ((2, "interests"),)),
    ("日常節奏與常見空閒時間", ((2, "routine"),)),
    ("希望角色怎麼陪伴或回應", ((2, "life_goals"),)),
)

_MIN_CONFIDENCE = {1: 0.7, 2: 0.7}
_RECENT_ATTEMPT_LIMIT = 8
_INTERACTION_HEAT_LABELS: dict[str, str] = {
    "stranger": "互動還很少",
    "acquaintance": "互動漸多",
    "familiar": "互動頻繁",
    "close": "互動很密切",
}


class PersonaCuriosityService:
    """Prepare facts for a future LLM curiosity planner.

    This service deliberately does not decide whether to ask. It
    converts the persona aggregate and recent ledger rows into compact
    semantic facts. Phase 2's LLM planner owns the actual decision.
    """

    def __init__(
        self,
        *,
        repository: PersonaCuriosityRepositoryPort,
    ) -> None:
        self._repository = repository

    async def build_context(
        self,
        *,
        persona: OperatorPersona,
        surface: str,
        recent_dialogue_summary: str = "",
        initial_relationship_lines: tuple[str, ...] = (),
        now: datetime | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> PersonaCuriosityContext:
        cleaned_surface = (surface or "").strip().lower()
        if cleaned_surface not in PERSONA_CURIOSITY_SURFACES:
            raise ValueError(
                "surface must be one of "
                f"{sorted(PERSONA_CURIOSITY_SURFACES)}",
            )
        recent_attempts = await self._repository.list_recent(
            persona.character_id,
            persona.operator_id,
            limit=_RECENT_ATTEMPT_LIMIT,
        )
        known = _known_profile_summary(persona)
        gaps = _profile_gaps(persona)
        return PersonaCuriosityContext(
            character_id=persona.character_id,
            operator_id=persona.operator_id,
            surface=cleaned_surface,
            known_profile_summary=known,
            profile_gaps=gaps,
            sensitive_boundaries=_sensitive_boundaries(),
            recent_curiosity_attempts=tuple(
                PersonaCuriosityAttemptSummary(
                    surface=attempt.surface,
                    target_layer=attempt.target_layer,
                    target_topic=attempt.target_topic,
                    question_intent=attempt.question_intent,
                    status=attempt.status,
                    created_at=attempt.created_at,
                )
                for attempt in recent_attempts
            ),
            recent_dialogue_summary=_clean_line(recent_dialogue_summary, 500),
            interaction_strength=_interaction_strength_summary(persona),
            initial_relationship_summary=_initial_relationship_summary(
                initial_relationship_lines,
            ),
            now=now,
            operator_primary_language=(operator_primary_language or "zh-TW"),
        )

    async def list_recent_attempts(
        self,
        character_id: str,
        operator_id: str,
        *,
        limit: int = 50,
    ) -> list[PersonaCuriosityAttempt]:
        return await self._repository.list_recent(
            character_id,
            operator_id,
            limit=limit,
        )

    async def record_planned_attempt(
        self,
        *,
        context: PersonaCuriosityContext,
        plan: PersonaCuriosityPlan,
        conversation_id: str | None = None,
        now: datetime | None = None,
    ) -> PersonaCuriosityAttempt | None:
        """Persist a planner intent so future LLM calls see recent attempts.

        This records only that the planner proposed an exploration intent.
        It does not claim the final assistant/proactive message adopted the
        intent; adoption remains a later LLM evidence/judge concern.
        """
        if (
            not plan.should_ask
            or plan.target_layer is None
            or not plan.target_topic.strip()
            or not plan.question_intent.strip()
        ):
            return None
        created_at = now or context.now or datetime.now(timezone.utc)
        attempt = PersonaCuriosityAttempt.new(
            character_id=context.character_id,
            operator_id=context.operator_id,
            conversation_id=conversation_id,
            surface=context.surface,
            target_layer=plan.target_layer,
            target_topic=plan.target_topic,
            question_intent=plan.question_intent,
            status=PERSONA_CURIOSITY_STATUS_PLANNED,
            created_at=created_at,
            metadata=_planned_attempt_metadata(plan),
        )
        return await self._repository.add(attempt)


def _known_profile_summary(persona: OperatorPersona) -> tuple[str, ...]:
    lines: list[str] = []
    for key, label in _LAYER1_LABELS.items():
        field = persona.layer1_identity.get(key)
        if _passes_safe_threshold(field):
            lines.append(f"{label}：{_clean_line(field.value, 160)}")
    for key, label in _LAYER2_LABELS.items():
        field = persona.layer2_life.get(key)
        if _passes_safe_threshold(field):
            lines.append(f"{label}：{_clean_line(field.value, 160)}")
    if lines:
        return tuple(lines)
    return ("目前還沒有穩定確認的使用者畫像。",)


def _profile_gaps(persona: OperatorPersona) -> tuple[str, ...]:
    gaps: list[str] = []
    for label, fields in _SAFE_GAP_GROUPS:
        if not any(_has_confirmed_field(persona, layer, key) for layer, key in fields):
            gaps.append(f"還不清楚使用者的{label}。")
    return tuple(gaps)


def _sensitive_boundaries() -> tuple[str, ...]:
    return (
        "Layer 1/2 可低壓探索；一次最多一個自然問題。",
        "Layer 3/5 屬於敏感資訊，除非使用者已主動打開話題，否則不要主動逼問。",
        "不要提到使用者畫像、資料蒐集、補欄位或問卷。",
    )


def _interaction_strength_summary(persona: OperatorPersona) -> str:
    strength = persona.layer4_interaction
    if strength is None or strength.first_message_at is None:
        return (
            "還沒有足夠互動紀錄；探索語氣應以最近對話與起始關係設定校準，"
            "不可因此覆寫關係主述。"
        )
    band = getattr(strength.familiarity_band, "value", "")
    label = _INTERACTION_HEAT_LABELS.get(str(band), "互動漸多")
    return f"互動熱度：{label}。"


def _initial_relationship_summary(lines: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = [
        line.strip()
        for line in lines
        if line and line.strip()
    ]
    if not cleaned:
        return ()
    return tuple([
        "起始關係設定是關係主述，互動量低時不可覆寫這份關係。",
        *cleaned[:8],
    ])


def _has_confirmed_field(persona: OperatorPersona, layer: int, key: str) -> bool:
    field = persona.fields_by_layer(layer).get(key)
    return _passes_safe_threshold(field)


def _passes_safe_threshold(field: ProfileField | None) -> bool:
    if field is None:
        return False
    if field.content_mode is MessageContentMode.NSFW:
        return False
    threshold = _MIN_CONFIDENCE.get(field.layer)
    return threshold is not None and field.confidence >= threshold


def _clean_line(value: str | None, max_chars: int) -> str:
    text = " ".join((value or "").strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _planned_attempt_metadata(plan: PersonaCuriosityPlan) -> dict:
    metadata = dict(plan.planner_metadata or {})
    return {
        "planner_metadata": _json_safe_dict(metadata),
        "tone_strategy": _clean_line(plan.tone_strategy, 160),
        "safety_reason": _clean_line(plan.safety_reason, 300),
        "avoid": [_clean_line(item, 120) for item in plan.avoid[:6] if item.strip()],
    }


def _json_safe_dict(raw: dict) -> dict:
    safe: dict = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[key] = value
        else:
            safe[key] = str(value)
    return safe
