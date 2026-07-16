"""LLM-backed novelty gate for generated chat replies."""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyGatePort,
    NoveltyVerdict,
)
from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.observability.llm_metadata_wrapper import (
    LLMCallMetadata,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_LINE_CHARS = 260
_MAX_LINES = 16
_MAX_RESPONSE_CHARS = 1600
_MAX_FEEDBACK_CHARS = 260


class LLMNoveltyGate(NoveltyGatePort):
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

    async def evaluate(
        self,
        context: NoveltyGateContext,
        *,
        character: Character | None = None,
    ) -> NoveltyVerdict:
        routed_character = character or _CharacterProxy(context)
        if await self._resolver.is_fake(
            character=routed_character,
            content_tolerance=context.content_tolerance,
        ):
            return NoveltyVerdict.pass_open("fake provider")
        prompt = _build_prompt(context)
        try:
            captured, provider_id = await self._resolver.generate_with_metadata(
                prompt,
                character=routed_character,
                content_tolerance=context.content_tolerance,
            )
            verdict = _parse_verdict(captured.text)
            if verdict is None:
                return NoveltyVerdict.pass_open("novelty gate returned invalid JSON")
            return _with_metadata(
                verdict,
                provider_id=provider_id,
                metadata=captured.metadata,
            )
        except Exception as exc:
            _LOGGER.exception(
                "novelty gate failed character=%s operator=%s",
                context.character_id,
                context.operator_id,
            )
            return NoveltyVerdict.pass_open(repr(exc))


class _CharacterProxy:
    def __init__(self, context: NoveltyGateContext) -> None:
        self.id = context.character_id
        self.user_id = context.operator_id
        self.feature_models = ()

    def feature_model_for(self, feature_key: str):  # noqa: ANN001
        return None


def _build_prompt(context: NoveltyGateContext) -> str:
    return get_default_loader().render(
        "novelty_gate/gate",
        content_tolerance=context.content_tolerance,
        latest_user_message=_clip(context.latest_user_message, 600) or "（無）",
        response_text=_clip(context.response_text, _MAX_RESPONSE_CHARS) or "（空）",
        known_material=_render_lines(context.known_material),
        recent_self_lines=_render_lines(context.recent_self_lines),
        self_repetition_hint=_clip(context.self_repetition_hint, 600) or "（無）",
        register_profile=_render_register_profile(context.register_profile),
        diversity_evidence=_render_diversity_evidence(context.diversity_evidence),
        persona_context=_render_lines(context.persona_context),
    )


def _render_lines(lines: tuple[str, ...]) -> str:
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return "- （無）"
    return "\n".join(
        f"- {_clip(line, _MAX_LINE_CHARS)}" for line in cleaned[:_MAX_LINES]
    )


def _parse_verdict(raw: str) -> NoveltyVerdict | None:
    obj = _extract_object(raw or "")
    if obj is None:
        return None
    passes = obj.get("passes")
    if not isinstance(passes, bool):
        return None
    lacks_novelty = obj.get("lacks_novelty")
    imagery_relapse = obj.get("imagery_relapse")
    register_mismatch = obj.get("register_mismatch")
    over_warm = obj.get("over_warm")
    formulaic = obj.get("formulaic")
    return NoveltyVerdict(
        passes=passes,
        lacks_novelty=lacks_novelty if isinstance(lacks_novelty, bool) else False,
        imagery_relapse=imagery_relapse if isinstance(imagery_relapse, bool) else False,
        register_mismatch=(
            register_mismatch if isinstance(register_mismatch, bool) else False
        ),
        over_warm=over_warm if isinstance(over_warm, bool) else False,
        formulaic=formulaic if isinstance(formulaic, bool) else False,
        feedback=_clip(
            obj.get("feedback") if isinstance(obj.get("feedback"), str) else "",
            _MAX_FEEDBACK_CHARS,
        ),
    )


def _with_metadata(
    verdict: NoveltyVerdict,
    *,
    provider_id: str,
    metadata: LLMCallMetadata,
) -> NoveltyVerdict:
    return NoveltyVerdict(
        passes=verdict.passes,
        lacks_novelty=verdict.lacks_novelty,
        imagery_relapse=verdict.imagery_relapse,
        register_mismatch=verdict.register_mismatch,
        over_warm=verdict.over_warm,
        formulaic=verdict.formulaic,
        feedback=verdict.feedback,
        gate_metadata={
            "enabled": True,
            "passes": verdict.passes,
            "provider_id": provider_id,
            "model_id": metadata.model_id,
            "latency_ms": metadata.latency_ms,
            "prompt_tokens": metadata.prompt_tokens,
            "completion_tokens": metadata.completion_tokens,
            "error": metadata.error,
        },
    )


def _render_register_profile(profile: RegisterProfile | None) -> str:
    if profile is None:
        return "- （未提供；視為中性日常語域）"
    axes = ", ".join(
        f"{name}={profile.axis(name):.2f}"
        for name in (
            "emotional_intensity",
            "seriousness",
            "intimacy",
            "humor_latitude",
            "help_seeking",
        )
    )
    vulnerable = "true" if profile.vulnerable_disclosure else "false"
    note = _clip(profile.note, 220) or "（無）"
    return "\n".join((
        f"- axes: {axes}",
        f"- confidence={profile.confidence:.2f}",
        f"- vulnerable_disclosure={vulnerable}",
        f"- note: {note}",
    ))


def _render_diversity_evidence(evidence: ReplyDiversityEvidence | None) -> str:
    if evidence is None:
        return "- （無統計證據）"
    lines = [
        f"- assistant_line_count={evidence.assistant_line_count}",
        f"- max_self_similarity={_fmt_optional(evidence.max_self_similarity)}",
        f"- mean_self_similarity={_fmt_optional(evidence.mean_self_similarity)}",
        "- self_repetition_hint: "
        + (_clip(evidence.self_repetition_hint, 360) or "（無）"),
    ]
    for item in evidence.phrase_frequency_lines[:6]:
        if item.strip():
            lines.append(f"- frequency: {_clip(item, 180)}")
    return "\n".join(lines)


def _fmt_optional(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


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


def _clip(raw: str, limit: int) -> str:
    text = " ".join((raw or "").strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
