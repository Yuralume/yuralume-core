"""LLM-backed planner for conversational persona discovery."""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.persona_curiosity import (
    PersonaCuriosityContext,
    PersonaCuriosityPlan,
    PersonaCuriosityPlannerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.observability.llm_metadata_wrapper import (
    LLMCallMetadata,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_TOPIC_CHARS = 80
_MAX_INTENT_CHARS = 300
_MAX_STRATEGY_CHARS = 120
_MAX_REASON_CHARS = 300
_MAX_AVOID_ITEMS = 6
_MAX_AVOID_CHARS = 120
_ALLOWED_TARGET_LAYERS = frozenset({1, 2, 3, 5})


class LLMPersonaCuriosityPlanner(PersonaCuriosityPlannerPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def plan(
        self,
        context: PersonaCuriosityContext,
        *,
        character: Character | None = None,
    ) -> PersonaCuriosityPlan:
        routed_character = character or _CharacterProxy(context)
        if await self._resolver.is_fake(character=routed_character):
            return PersonaCuriosityPlan.no_ask("fake provider")
        prompt = _build_prompt(context)
        try:
            captured, provider_id = await self._resolver.generate_with_metadata(
                prompt,
                character=routed_character,
            )
            plan = _parse_plan(captured.text)
            return _with_planner_metadata(
                plan,
                context=context,
                provider_id=provider_id,
                metadata=captured.metadata,
            )
        except Exception as exc:
            log_auxiliary_llm_failure(
                _LOGGER, exc,
                "persona curiosity planner failed character=%s operator=%s",
                context.character_id,
                context.operator_id,
            )
            return PersonaCuriosityPlan.no_ask()


class NullPersonaCuriosityPlanner(PersonaCuriosityPlannerPort):
    async def plan(
        self,
        context: PersonaCuriosityContext,
        *,
        character: Character | None = None,
    ) -> PersonaCuriosityPlan:
        del character
        return PersonaCuriosityPlan.no_ask("planner disabled")


class _CharacterProxy:
    """Tiny proxy so ActiveLLMProvider can honour per-character overrides."""

    def __init__(self, context: PersonaCuriosityContext) -> None:
        self.id = context.character_id
        self.user_id = context.operator_id
        self.feature_models = ()

    def feature_model_for(self, feature_key: str):  # noqa: ANN001
        return None


def _build_prompt(context: PersonaCuriosityContext) -> str:
    return get_default_loader().render(
        "persona/curiosity_planner",
        # ``question_intent`` renders in the Observability "current intent"
        # panel, so it must follow the operator's content language instead of
        # the prompt's Chinese scaffolding.
        language_hint=render_operator_language_hint(
            context.operator_primary_language,
        ),
        surface=context.surface,
        now=context.now.isoformat(timespec="minutes") if context.now else "unknown",
        interaction_strength=_render_interaction_context(context),
        recent_dialogue=context.recent_dialogue_summary or "（無）",
        known_profile=_render_lines(context.known_profile_summary),
        profile_gaps=_render_lines(context.profile_gaps),
        sensitive_boundaries=_render_lines(context.sensitive_boundaries),
        recent_attempts=_render_attempts(context),
    )


def _render_interaction_context(context: PersonaCuriosityContext) -> str:
    lines: list[str] = []
    if context.interaction_strength:
        lines.append(context.interaction_strength)
    if context.initial_relationship_summary:
        lines.extend(context.initial_relationship_summary)
    if not lines:
        return "（無）"
    return "\n".join(f"- {line[:240]}" for line in lines if line.strip())


def _render_lines(lines: tuple[str, ...]) -> str:
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return "- （無）"
    return "\n".join(f"- {line[:240]}" for line in cleaned[:10])


def _render_attempts(context: PersonaCuriosityContext) -> str:
    if not context.recent_curiosity_attempts:
        return "- （無）"
    lines: list[str] = []
    for attempt in context.recent_curiosity_attempts[:8]:
        created_at = attempt.created_at.isoformat(timespec="minutes")
        lines.append(
            "- "
            f"{created_at} | surface={attempt.surface} | "
            f"layer={attempt.target_layer} | topic={attempt.target_topic[:80]} | "
            f"status={attempt.status} | intent={attempt.question_intent[:180]}"
        )
    return "\n".join(lines)


def _parse_plan(raw: str) -> PersonaCuriosityPlan:
    obj = _extract_object(raw or "")
    if obj is None:
        return PersonaCuriosityPlan.no_ask()
    should_ask = obj.get("should_ask") is True
    if not should_ask:
        return PersonaCuriosityPlan(
            should_ask=False,
            safety_reason=_trim(obj.get("safety_reason"), _MAX_REASON_CHARS)
            or "planner chose not to ask",
            avoid=tuple(_valid_text_list(obj.get("avoid"))),
        )
    layer = _parse_layer(obj.get("target_layer"))
    target_topic = _trim(obj.get("target_topic"), _MAX_TOPIC_CHARS)
    tone_strategy = _trim(obj.get("tone_strategy"), _MAX_STRATEGY_CHARS)
    question_intent = _trim(obj.get("question_intent"), _MAX_INTENT_CHARS)
    safety_reason = _trim(obj.get("safety_reason"), _MAX_REASON_CHARS)
    if (
        layer is None
        or not target_topic
        or not tone_strategy
        or not question_intent
        or not safety_reason
    ):
        return PersonaCuriosityPlan.no_ask()
    return PersonaCuriosityPlan(
        should_ask=True,
        target_layer=layer,
        target_topic=target_topic,
        tone_strategy=tone_strategy,
        question_intent=question_intent,
        safety_reason=safety_reason,
        avoid=tuple(_valid_text_list(obj.get("avoid"))),
    )


def _with_planner_metadata(
    plan: PersonaCuriosityPlan,
    *,
    context: PersonaCuriosityContext,
    provider_id: str,
    metadata: LLMCallMetadata,
) -> PersonaCuriosityPlan:
    planner_metadata = {
        "surface": context.surface,
        "provider_id": provider_id,
        "model_id": metadata.model_id,
        "latency_ms": metadata.latency_ms,
        "prompt_tokens": metadata.prompt_tokens,
        "completion_tokens": metadata.completion_tokens,
        "error": metadata.error,
        "recent_attempt_count": len(context.recent_curiosity_attempts),
    }
    return PersonaCuriosityPlan(
        should_ask=plan.should_ask,
        target_layer=plan.target_layer,
        target_topic=plan.target_topic,
        tone_strategy=plan.tone_strategy,
        question_intent=plan.question_intent,
        safety_reason=plan.safety_reason,
        avoid=plan.avoid,
        planner_metadata=planner_metadata,
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
                    parsed = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _parse_layer(raw: Any) -> int | None:
    if isinstance(raw, int):
        layer = raw
    elif isinstance(raw, str):
        cleaned = raw.strip().lower()
        if cleaned.startswith("layer"):
            cleaned = cleaned[5:]
        try:
            layer = int(cleaned)
        except ValueError:
            return None
    else:
        return None
    return layer if layer in _ALLOWED_TARGET_LAYERS else None


def _trim(raw: Any, limit: int) -> str:
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.strip().strip("「」\"'").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _valid_text_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    items: list[str] = []
    for value in raw:
        text = _trim(value, _MAX_AVOID_CHARS)
        if text:
            items.append(text)
        if len(items) >= _MAX_AVOID_ITEMS:
            break
    return items
