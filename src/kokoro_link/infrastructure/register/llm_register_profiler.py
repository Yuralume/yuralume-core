"""LLM-backed per-turn register profiler."""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.feature_keys import FEATURE_REGISTER_PROFILE
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.register_profile import (
    RegisterProfile,
    RegisterProfileContext,
    RegisterProfilePort,
    normalise_axes,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.observability.llm_metadata_wrapper import (
    LLMCallMetadata,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 1000
_MAX_SUMMARY_CHARS = 900
_MAX_NOTE_CHARS = 180


class LLMRegisterProfiler(RegisterProfilePort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str = FEATURE_REGISTER_PROFILE,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def profile(
        self,
        context: RegisterProfileContext,
        *,
        character: Character | None = None,
    ) -> RegisterProfile | None:
        routed_character = character or _CharacterProxy(context)
        if await self._resolver.is_fake(
            character=routed_character,
            content_tolerance=context.content_tolerance,
        ):
            return None
        prompt = _build_prompt(context)
        try:
            captured, provider_id = await self._resolver.generate_with_metadata(
                prompt,
                character=routed_character,
                content_tolerance=context.content_tolerance,
            )
            profile = _parse_profile(captured.text)
            if profile is None:
                return None
            return _with_metadata(
                profile,
                provider_id=provider_id,
                metadata=captured.metadata,
            )
        except Exception:
            _LOGGER.exception(
                "register profiler failed character=%s operator=%s",
                context.character_id,
                context.operator_id,
            )
            return None


class _CharacterProxy:
    def __init__(self, context: RegisterProfileContext) -> None:
        self.id = context.character_id
        self.user_id = context.operator_id
        self.feature_models = ()

    def feature_model_for(self, feature_key: str):  # noqa: ANN001
        return None


def _build_prompt(context: RegisterProfileContext) -> str:
    return get_default_loader().render(
        "register/profiler",
        content_tolerance=context.content_tolerance,
        latest_user_message=_clip(context.latest_user_message, _MAX_MESSAGE_CHARS) or "（無）",
        recent_dialogue_summary=_clip(
            context.recent_dialogue_summary,
            _MAX_SUMMARY_CHARS,
        ) or "（無）",
        relationship_context=_render_lines(context.relationship_context),
    )


def _parse_profile(raw: str) -> RegisterProfile | None:
    obj = _extract_object(raw or "")
    if obj is None:
        return None
    if not isinstance(obj.get("axes"), dict):
        return None
    axes = normalise_axes(obj.get("axes"))
    confidence_raw = obj.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    vulnerable = obj.get("vulnerable_disclosure", False)
    note = obj.get("note", "")
    return RegisterProfile(
        axes=axes,
        confidence=confidence,
        note=_clip(note if isinstance(note, str) else "", _MAX_NOTE_CHARS),
        vulnerable_disclosure=vulnerable if isinstance(vulnerable, bool) else False,
    )


def _with_metadata(
    profile: RegisterProfile,
    *,
    provider_id: str,
    metadata: LLMCallMetadata,
) -> RegisterProfile:
    return RegisterProfile(
        axes=dict(profile.axes),
        confidence=profile.confidence,
        note=profile.note,
        vulnerable_disclosure=profile.vulnerable_disclosure,
        metadata={
            "enabled": True,
            "provider_id": provider_id,
            "model_id": metadata.model_id,
            "latency_ms": metadata.latency_ms,
            "prompt_tokens": metadata.prompt_tokens,
            "completion_tokens": metadata.completion_tokens,
            "error": metadata.error,
        },
    )


def _render_lines(lines: tuple[str, ...]) -> str:
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return "- （無）"
    return "\n".join(f"- {_clip(line, 180)}" for line in cleaned[:8])


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


def _clip(raw: object, limit: int) -> str:
    text = " ".join(str(raw or "").strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
