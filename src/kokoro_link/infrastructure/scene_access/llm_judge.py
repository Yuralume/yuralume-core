from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.scene_access import (
    SceneAccessContext,
    SceneAccessJudgePort,
    StageAccessAction,
    StageAccessDecision,
    StageAccessVerdict,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.presence_frame import AccessContext
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_REASON_CHARS = 240
_MAX_PROMPT_FACT_CHARS = 360
_MAX_OPENER_CHARS = 120


class SceneAccessJudgeError(RuntimeError):
    pass


class LLMSceneAccessJudge(SceneAccessJudgePort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def judge(self, context: SceneAccessContext) -> StageAccessVerdict:
        if await self._resolver.is_fake(character=_CharacterProxy(context)):
            raise SceneAccessJudgeError("scene access judge routed to fake model")
        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(
                prompt,
                character=_CharacterProxy(context),
            )
        except Exception as exc:
            log_auxiliary_llm_failure(
                _LOGGER, exc,
                "scene access LLM call failed character=%s",
                context.character_id,
            )
            raise SceneAccessJudgeError("scene access LLM call failed") from exc
        return _parse_response(raw)


class _CharacterProxy:
    """Tiny proxy so ActiveLLMProvider can honour per-character overrides."""

    def __init__(self, context: SceneAccessContext) -> None:
        self.id = context.character_id
        self.user_id = context.operator_id
        self.feature_models = ()

    def feature_model_for(self, feature_key: str):  # noqa: ANN001
        return None


def _build_prompt(context: SceneAccessContext) -> str:
    return get_default_loader().render(
        "scene_access/judge",
        character_name=context.character_name,
        character_summary=context.character_summary or "（未設定）",
        character_boundaries=_render_list(context.character_boundaries),
        familiarity_band=context.familiarity_band,
        trust_band=context.trust_band,
        requested_surface=context.requested_surface.value,
        operator_language_hint=render_operator_language_hint(
            context.operator_primary_language,
        ),
        now_local=(
            context.now_local.isoformat(timespec="minutes")
            if context.now_local is not None else "unknown"
        ),
        activity_summary=context.current_activity_summary or "（目前沒有明確活動）",
        activity_location=context.current_activity_location or "（未知）",
        activity_category=context.current_activity_category or "（未知）",
        activity_busy_score=(
            f"{context.current_activity_busy_score:.2f}"
            if context.current_activity_busy_score is not None else "unknown"
        ),
        activity_scene_privacy=context.current_activity_scene_privacy or "unknown",
        activity_meeting_affordance=(
            context.current_activity_meeting_affordance or "unknown"
        ),
        schedule_context_summary=(
            context.schedule_context_summary or "（無補充行程上下文）"
        ),
        recent_dialogue=_render_list(context.recent_dialogue),
        operator_current_status=context.operator_current_status or "（未設定）",
        operator_current_status_set_at=(
            context.operator_current_status_set_at.isoformat(timespec="minutes")
            if context.operator_current_status_set_at is not None
            else "unknown"
        ),
        initial_relationship=_render_list(context.initial_relationship_lines),
        operator_persona=_render_list(context.operator_persona_lines),
        evidence=_render_list(context.recent_invitation_or_meetup_evidence),
    )


def _render_list(items: tuple[str, ...]) -> str:
    cleaned = [item.strip() for item in items if item and item.strip()]
    if not cleaned:
        return "- （無）"
    return "\n".join(f"- {item[:240]}" for item in cleaned[:8])


def _parse_response(raw: str) -> StageAccessVerdict:
    obj = _extract_object(raw or "")
    if obj is None:
        raise SceneAccessJudgeError("scene access LLM returned no JSON object")
    try:
        decision = StageAccessDecision(_coerce_str(obj.get("decision")).lower())
        action = StageAccessAction(_coerce_str(obj.get("recommended_action")).lower())
        access_context = AccessContext(_coerce_str(obj.get("access_context")).lower())
    except ValueError as exc:
        raise SceneAccessJudgeError("scene access LLM returned invalid enum") from exc
    reason = _trim(obj.get("reason_for_user"), _MAX_REASON_CHARS)
    prompt_fact = _trim(obj.get("prompt_fact"), _MAX_PROMPT_FACT_CHARS)
    if not reason or not prompt_fact:
        raise SceneAccessJudgeError("scene access LLM returned missing explanation")
    suggested = _trim(obj.get("suggested_opener"), _MAX_OPENER_CHARS) or None
    return StageAccessVerdict(
        decision=decision,
        recommended_action=action,
        access_context=access_context,
        reason_for_user=reason,
        prompt_fact=prompt_fact,
        suggested_opener=suggested,
    )


def _extract_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object embedded in ``text``.

    Models sometimes wrap the verdict in prose or a ```json fence, or emit a
    stray brace (an aside, an example) before the real object. Scanning every
    ``{`` candidate left-to-right — instead of giving up the moment the first
    balanced span fails to parse — keeps the gate working on those variants
    without hard-coding any specific wrapper.
    """
    search_from = 0
    while True:
        start = text.find("{", search_from)
        if start == -1:
            return None
        span = _balanced_span(text, start)
        if span is not None:
            try:
                parsed = json.loads(span)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        search_from = start + 1


def _balanced_span(text: str, start: int) -> str | None:
    """Substring from ``start`` to its matching ``}``, honouring strings."""
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


def _coerce_str(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _trim(raw: Any, limit: int) -> str:
    if not isinstance(raw, str):
        return ""
    text = raw.strip().strip("「」\"'")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


_ = Character
