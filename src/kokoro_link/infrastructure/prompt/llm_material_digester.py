"""LLM-backed prompt material digester."""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigest,
    PromptMaterialDigestContext,
    PromptMaterialDigestPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.observability.llm_metadata_wrapper import (
    LLMCallMetadata,
)
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.llm_output import extract_object_outcome, log_parse_outcome

_LOGGER = logging.getLogger(__name__)

_MAX_INPUT_LINE_CHARS = 360
_MAX_INPUT_LINES = 12
_MAX_BULLETS = 12
_MAX_BULLET_CHARS = 220


class LLMPromptMaterialDigester(PromptMaterialDigestPort):
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

    async def digest(
        self,
        context: PromptMaterialDigestContext,
        *,
        character: Character | None = None,
    ) -> PromptMaterialDigest | None:
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
            digest = _parse_digest(captured.text)
            if digest is None:
                return None
            return _with_metadata(
                digest,
                context=context,
                provider_id=provider_id,
                metadata=captured.metadata,
            )
        except Exception:
            _LOGGER.exception(
                "prompt material digest failed character=%s operator=%s",
                context.character_id,
                context.operator_id,
            )
            return None


class _CharacterProxy:
    """Tiny proxy so ActiveLLMProvider can honour per-character overrides."""

    def __init__(self, context: PromptMaterialDigestContext) -> None:
        self.id = context.character_id
        self.user_id = context.operator_id
        self.feature_models = ()

    def feature_model_for(self, feature_key: str):  # noqa: ANN001
        return None


def _build_prompt(context: PromptMaterialDigestContext) -> str:
    return get_default_loader().render(
        "prompt/material_digest",
        source_language=context.source_language or "same as source material",
        content_tolerance=context.content_tolerance,
        emotion_events=_render_lines(context.emotion_events),
        self_reflections=_render_lines(context.self_reflections),
        story_events=_render_lines(context.story_events),
        story_arc=_render_lines(context.story_arc),
        recent_feed_posts=_render_lines(context.recent_feed_posts),
    )


def _render_lines(lines: tuple[str, ...]) -> str:
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return "- （無）"
    return "\n".join(
        f"- {_clip(line, _MAX_INPUT_LINE_CHARS)}"
        for line in cleaned[:_MAX_INPUT_LINES]
    )


def _parse_digest(raw: str) -> PromptMaterialDigest | None:
    outcome = extract_object_outcome(raw or "")
    log_parse_outcome(_LOGGER, outcome, site="prompt.llm_material_digester")
    obj = outcome.value
    if obj is None:
        return None
    bullets = tuple(_valid_bullets(obj.get("bullets")))
    if not bullets:
        return None
    return PromptMaterialDigest(bullets=bullets)


def _with_metadata(
    digest: PromptMaterialDigest,
    *,
    context: PromptMaterialDigestContext,
    provider_id: str,
    metadata: LLMCallMetadata,
) -> PromptMaterialDigest:
    return PromptMaterialDigest(
        bullets=digest.bullets,
        digest_metadata={
            "enabled": True,
            "applied": True,
            "bullet_count": len(digest.bullets),
            "provider_id": provider_id,
            "model_id": metadata.model_id,
            "latency_ms": metadata.latency_ms,
            "prompt_tokens": metadata.prompt_tokens,
            "completion_tokens": metadata.completion_tokens,
            "error": metadata.error,
            "content_tolerance": context.content_tolerance,
        },
    )


def _valid_bullets(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    bullets: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        text = _clip(" ".join(value.strip().split()), _MAX_BULLET_CHARS)
        if text:
            bullets.append(text)
        if len(bullets) >= _MAX_BULLETS:
            break
    return bullets


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
