"""LLM-backed novelty gate for generated chat replies."""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.novelty_gate import (
    ALL_AXES as _VERDICT_AXES,
    HARD_AXES as _HARD_AXES,
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
from kokoro_link.llm_output import extract_object_outcome, log_parse_outcome

_LOGGER = logging.getLogger(__name__)

_MAX_LINE_CHARS = 260
_MAX_LINES = 16
_MAX_RESPONSE_CHARS = 1600
_MAX_FEEDBACK_CHARS = 260
# Tool prompts (image/video) are tag soup and get judged as a whole; a
# 260-char clip would hide the very tail that leaked into the body.
_MAX_TOOL_PROMPT_CHARS = 480
_MAX_LANGUAGE_CHARS = 60
_MAX_EVIDENCE_ITEMS = 6
"""How many items of one statistical-evidence kind the block names.

Evidence competes for prompt budget with the material the reply is
actually about, so each kind gets the same small allowance rather than
whatever its producer happened to hand over."""
_MAX_EVIDENCE_ITEM_CHARS = 180

_MAX_TEMPORAL_LINES = 8
"""The 時間座標 block is a short list of anchors, not a timeline dump."""


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
            # Not ``pass_open``: that spells "a judge exists and broke" and
            # the orchestrator counts it as a fail-open. No routable judge
            # is "no review happened" — uncounted, like an unwired gate.
            return NoveltyVerdict.pass_unrouted()
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
        operator_primary_language=(
            _clip(context.operator_primary_language, _MAX_LANGUAGE_CHARS) or "（無）"
        ),
        tool_prompts=_render_lines(
            context.tool_prompt_lines,
            limit=_MAX_TOOL_PROMPT_CHARS,
        ),
        mechanical_evidence=_render_lines(context.mechanical_evidence_lines),
        # Kept out of ``mechanical_evidence`` on purpose: the rubric pins
        # ``temporal_inconsistency`` false when this block is empty, which
        # only works if "the caller supplied no time anchors" stays
        # distinguishable from "the caller supplied a length warning".
        temporal_context=_render_lines(
            context.temporal_context_lines,
            max_lines=_MAX_TEMPORAL_LINES,
        ),
    )


def _render_lines(
    lines: tuple[str, ...],
    *,
    limit: int = _MAX_LINE_CHARS,
    max_lines: int = _MAX_LINES,
) -> str:
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return "- （無）"
    return "\n".join(f"- {_clip(line, limit)}" for line in cleaned[:max_lines])


def _parse_verdict(raw: str) -> NoveltyVerdict | None:
    outcome = extract_object_outcome(raw or "")
    log_parse_outcome(_LOGGER, outcome, site="prompt.llm_novelty_gate")
    obj = outcome.value
    if obj is None:
        return None
    passes = obj.get("passes")
    if not isinstance(passes, bool):
        return None
    return NoveltyVerdict(
        passes=passes,
        feedback=_clip(
            obj.get("feedback") if isinstance(obj.get("feedback"), str) else "",
            _MAX_FEEDBACK_CHARS,
        ),
        **{axis: _flag(obj, axis) for axis in _VERDICT_AXES},
    )


def _flag(obj: dict[str, Any], key: str) -> bool:
    """A missing or non-boolean axis reads as ``False``.

    An older judge (or an older tuned overlay that has not grown the hard
    axes yet) simply omits them; treating that as "not fired" keeps the
    gate fail-soft instead of turning a pack lag into a blocked surface.
    """
    value = obj.get(key)
    return value if isinstance(value, bool) else False


def _with_metadata(
    verdict: NoveltyVerdict,
    *,
    provider_id: str,
    metadata: LLMCallMetadata,
) -> NoveltyVerdict:
    return NoveltyVerdict(
        passes=verdict.passes,
        feedback=verdict.feedback,
        gate_metadata={
            "enabled": True,
            "passes": verdict.passes,
            "hard_fail": verdict.hard_fail,
            **{axis: getattr(verdict, axis) for axis in _HARD_AXES},
            "provider_id": provider_id,
            "model_id": metadata.model_id,
            "latency_ms": metadata.latency_ms,
            "prompt_tokens": metadata.prompt_tokens,
            "completion_tokens": metadata.completion_tokens,
            "error": metadata.error,
        },
        **{axis: getattr(verdict, axis) for axis in _VERDICT_AXES},
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
    """The 統計多樣性證據 block, from every field the evidence carries.

    Both item kinds are rendered, and that is the whole point of doing it
    here: ``language_mix_lines`` is the deterministic material the
    ``language_mismatch`` axis is supposed to weigh, and dropping it made
    this channel half-wired. Chat did not notice because it also ships the
    same lines through ``mechanical_evidence_lines``; any surface that
    fills only the diversity field had its evidence discarded silently
    between the context and the prompt.
    """
    if evidence is None:
        return "- （無統計證據）"
    lines = [
        f"- assistant_line_count={evidence.assistant_line_count}",
        f"- max_self_similarity={_fmt_optional(evidence.max_self_similarity)}",
        f"- mean_self_similarity={_fmt_optional(evidence.mean_self_similarity)}",
        "- self_repetition_hint: "
        + (_clip(evidence.self_repetition_hint, 360) or "（無）"),
    ]
    lines.extend(_render_evidence_items("frequency", evidence.phrase_frequency_lines))
    lines.extend(_render_evidence_items("language_mix", evidence.language_mix_lines))
    return "\n".join(lines)


def _render_evidence_items(label: str, items: tuple[str, ...]) -> list[str]:
    """One labelled bullet per item, capped the same way for every kind."""
    return [
        f"- {label}: {_clip(item, _MAX_EVIDENCE_ITEM_CHARS)}"
        for item in items[:_MAX_EVIDENCE_ITEMS]
        if item.strip()
    ]


def _fmt_optional(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


def _clip(raw: str, limit: int) -> str:
    text = " ".join((raw or "").strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
