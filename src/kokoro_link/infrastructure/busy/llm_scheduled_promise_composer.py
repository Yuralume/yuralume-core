"""LLM-backed adapter for the scheduled-promise composer port.

Builds a Chinese prompt that lays out the character's persona, the
original promise the user asked for, the current activity context, and
asks the model to write the actual message that fulfils the promise.

Output is plain prose. Same normalisation + length cap as
:class:`LLMPendingFollowUpComposer` (they share the same fail-soft
contract: empty output = retry next tick).

**Tool passes (PF1).** When the caller offers ``available_tools``, the
prompt gains the shared tools block and the model may answer with a
JSON tool call instead of prose; we parse it and return it as
``tool_calls`` for the application layer to execute — this adapter
never runs a tool itself. On the second pass the caller supplies
``tool_results`` (successes *and* failures) and no tools, so the model
writes the message from what it actually got. Any JSON emitted when no
tools are on offer is suppressed rather than shipped: a player must
never receive a raw ``{"tool": …}`` blob as their promised message.

**Machine output is never the message — on any pass.** The guard for
that belongs to "this text is about to become player-visible", not to
"this pass offered tools", so it runs on every compose. Only its
*width* depends on the pass: with tools on offer the prompt demanded
JSON alone, so anything carrying a call's shape is a failed call
(:func:`looks_like_tool_call_shape`); without them we only refuse an
answer that is *entirely* a structured literal
(:func:`looks_like_object_literal`), because there the model was asked
for prose and over-refusing would silently drop a kept promise.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.tool_call_parser import (
    looks_like_object_literal,
    looks_like_tool_call_attempt,
    looks_like_tool_call_shape,
    parse_tool_call,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
    ScheduledPromiseComposerPort,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_COMMUNITY,
    CONTENT_TOLERANCE_FRONTIER,
    content_tolerance_for_llm_provider,
    normalize_content_tolerance,
    requires_community_routing_for_unreplaceable_nsfw,
)
from kokoro_link.domain.value_objects.timezone import to_timezone
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    render_current_time_fact_lines,
)
from kokoro_link.infrastructure.prompt.tool_outcomes_block import (
    TOOL_PASS_CLOSING_HINT,
    render_tool_outcomes_block,
)
from kokoro_link.infrastructure.prompt.tools_block import render_tools_block
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_REPLY_CHARS = 600
"""Cap on the rendered message. Tighter than the busy-defer composer
because a scheduled promise is usually short ("早安!該起床囉~") —
800-char essays here would feel bizarre."""

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class LLMScheduledPromiseComposer(ScheduledPromiseComposerPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def compose(
        self, payload: ScheduledPromiseComposeInput,
    ) -> ScheduledPromiseComposeOutput:
        if not payload.promise_intent.strip():
            return ScheduledPromiseComposeOutput(content_text="")
        routing_tolerance = _routing_tolerance_for_payload(
            payload,
            supports_routing=self._resolver.supports_content_tolerance_routing,
        )
        try:
            if await self._resolver.is_fake(
                character=payload.character,
                content_tolerance=routing_tolerance,
            ):
                return ScheduledPromiseComposeOutput(content_text="")
            model, model_id = await self._resolver.resolve(
                character=payload.character,
                content_tolerance=routing_tolerance,
            )
        except Exception:
            _LOGGER.exception(
                "scheduled-promise composer route resolve failed character=%s",
                payload.character.id,
            )
            return ScheduledPromiseComposeOutput(content_text="")
        content_tolerance = routing_tolerance or content_tolerance_for_llm_provider(
            getattr(model, "provider_id", ""),
        )
        prompt = _build_prompt(
            replace(payload, content_tolerance=content_tolerance),
        )
        try:
            kwargs = {"model": model_id} if model_id is not None else {}
            raw = await model.generate(prompt, **kwargs)
        except Exception:
            _LOGGER.exception(
                "scheduled-promise composer LLM call failed character=%s",
                payload.character.id,
            )
            return ScheduledPromiseComposeOutput(content_text="")
        return _output_for(raw, payload)


def _output_for(
    raw: str, payload: ScheduledPromiseComposeInput,
) -> ScheduledPromiseComposeOutput:
    """Split the model's raw answer into "a tool call" or "the message".

    Parsing runs against ``raw`` rather than the normalised text because
    normalisation strips fences and truncates at 600 chars — either can
    mangle a JSON call before we ever look at it.
    """
    allowed = {tool.name for tool in payload.available_tools}
    if allowed:
        call = parse_tool_call(raw)
        if call is not None and call.name in allowed:
            return ScheduledPromiseComposeOutput(
                content_text="", tool_calls=(call,),
            )
        if call is not None:
            _LOGGER.info(
                "scheduled-promise composer asked for unavailable tool %r "
                "character=%s — treating as no output",
                call.name, payload.character.id,
            )
            return ScheduledPromiseComposeOutput(content_text="")
    # Runs on *every* pass, at the width that pass has earned. The second
    # pass offers no tools, so it lands here with the narrow judgement —
    # which is the whole point: that is the pass whose output is handed
    # straight to the player, and it used to have no guard at all.
    if (
        looks_like_tool_call_shape(raw)
        if allowed
        else looks_like_object_literal(raw)
    ):
        _LOGGER.warning(
            "scheduled-promise composer returned machine output instead of a "
            "message character=%s tools_offered=%d — suppressing",
            payload.character.id, len(allowed),
        )
        return ScheduledPromiseComposeOutput(content_text="")
    if looks_like_tool_call_attempt(raw):
        # Either a malformed call, or a call on the pass where no tools
        # were offered. Shipping the blob would put raw JSON in the
        # player's chat; empty text means "retry next tick" instead.
        _LOGGER.warning(
            "scheduled-promise composer emitted an unusable tool call "
            "character=%s tools_offered=%d — suppressing",
            payload.character.id, len(allowed),
        )
        return ScheduledPromiseComposeOutput(content_text="")
    return ScheduledPromiseComposeOutput(content_text=_normalize(raw))


class NullScheduledPromiseComposer(ScheduledPromiseComposerPort):
    """Always emits empty output — used by the fake-provider path so
    scheduled promises stay silently queued in dev runs."""

    async def compose(
        self, payload: ScheduledPromiseComposeInput,
    ) -> ScheduledPromiseComposeOutput:
        return ScheduledPromiseComposeOutput(content_text="")


def _build_prompt(payload: ScheduledPromiseComposeInput) -> str:
    character = payload.character
    persona = "\n".join(_persona_block(character))
    operator_block_lines = _operator_persona_block(payload.operator_persona_lines)
    operator_block = "\n" + "\n".join(operator_block_lines) if operator_block_lines else ""
    schedule_block = "\n".join(_schedule_block(payload))
    summary = (payload.recent_dialogue_summary or "").strip()
    summary_block = "\n\n最近對話脈絡：\n" + summary[:400] if summary else ""
    original_text = _promise_text_for_tolerance(
        payload.promise_text,
        content_mode=payload.promise_content_mode,
        safe_summary=payload.promise_safe_summary,
        content_tolerance=payload.content_tolerance,
    )
    original_block = (
        f"\n\n對方當初的原話：「{original_text[:200]}」" if original_text else ""
    )
    scheduled_local = to_timezone(payload.scheduled_for, payload.local_tz)
    tools_block = _tools_block(payload)
    body = get_default_loader().render(
        "busy/scheduled_promise_composer",
        promise_intent=payload.promise_intent.strip()[:300],
        scheduled_at_local=scheduled_local.strftime("%Y-%m-%d %H:%M"),
        original_block=original_block,
        persona_block=persona,
        operator_persona_block=operator_block,
        schedule_block=schedule_block,
        summary_block=summary_block,
        tool_results_block=_tool_results_block(payload),
        tools_block=tools_block,
        closing_tool_hint=(
            f"{TOOL_PASS_CLOSING_HINT}\n" if tools_block else ""
        ),
        max_reply_chars=_MAX_REPLY_CHARS,
    )
    language_hint = render_operator_language_hint(
        payload.operator_primary_language,
    )
    if language_hint:
        body = f"{language_hint}\n\n{body}"
    return body


def _tools_block(payload: ScheduledPromiseComposeInput) -> str:
    """The shared chat/proactive tools section, first pass only.

    Empty on every pre-PF1 call and on the second pass — the tool has
    already run by then, and re-offering it invites a second call the
    loop would not execute."""
    if not payload.available_tools:
        return ""
    lines = render_tools_block(list(payload.available_tools))
    return "\n\n" + "\n".join(lines) if lines else ""


def _tool_results_block(payload: ScheduledPromiseComposeInput) -> str:
    if not payload.tool_results:
        return ""
    lines = render_tool_outcomes_block(list(payload.tool_results))
    return "\n\n" + "\n".join(lines) if lines else ""


def _routing_tolerance_for_payload(
    payload: ScheduledPromiseComposeInput,
    *,
    supports_routing: bool,
) -> str | None:
    if not supports_routing:
        return None
    item = _PromiseContentItem(
        content_mode=payload.promise_content_mode,
        safe_summary=payload.promise_safe_summary,
    )
    if requires_community_routing_for_unreplaceable_nsfw((item,)):
        return CONTENT_TOLERANCE_COMMUNITY
    return None


class _PromiseContentItem:
    def __init__(
        self,
        *,
        content_mode: MessageContentMode,
        safe_summary: str,
    ) -> None:
        self.content_mode = content_mode
        self.safe_summary = safe_summary


def _promise_text_for_tolerance(
    promise_text: str,
    *,
    content_mode: MessageContentMode,
    safe_summary: str = "",
    content_tolerance: str,
) -> str:
    if _should_use_safe_summary(
        content_mode,
        content_tolerance=content_tolerance,
    ):
        return (safe_summary or "").strip()
    return (promise_text or "").strip()


def _should_use_safe_summary(
    content_mode: MessageContentMode,
    *,
    content_tolerance: str,
) -> bool:
    return (
        normalize_content_tolerance(content_tolerance) == CONTENT_TOLERANCE_FRONTIER
        and content_mode is MessageContentMode.NSFW
    )


def _persona_block(character) -> list[str]:
    lines = [f"- 名稱：{character.name}"]
    lines.extend(render_character_identity_lines(character))
    if character.summary:
        lines.append(f"- 簡介：{character.summary[:200]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:6]))
    if character.speaking_style:
        lines.append(f"- 說話風格：{character.speaking_style[:160]}")
    lines.extend(character.disposition.to_prompt_lines())
    lines.extend(character.personality_type.to_prompt_lines())
    state = character.state
    lines.append(
        f"- 當前情緒：{state.emotion}（好感 {state.affection}/精力 "
        f"{state.energy}/疲勞 {state.fatigue}）",
    )
    return lines


def _operator_persona_block(lines: tuple[str, ...]) -> list[str]:
    cleaned = [line for line in lines if line.strip()]
    if not cleaned:
        return []
    return [
        "",
        "你對對方逐步認識到的事（只當背景，不要每次都主動提起）：",
        *cleaned,
        "- 履行承諾時只在自然相關時使用這些資訊；不要把畫像內容硬塞進提醒。",
    ]


def _schedule_block(payload: ScheduledPromiseComposeInput) -> list[str]:
    lines = ["活動脈絡："]
    lines.extend(
        render_current_time_fact_lines(payload.now, payload.local_tz, heading=None),
    )
    if payload.just_finished_activity is not None:
        activity = payload.just_finished_activity
        loc = f"（{activity.location}）" if activity.location else ""
        lines.append(
            f"- 剛結束：{activity.category} — {activity.description}{loc}",
        )
    if payload.current_activity is not None:
        activity = payload.current_activity
        loc = f"（{activity.location}）" if activity.location else ""
        lines.append(
            f"- 現在進行：{activity.category} — {activity.description}{loc}"
            f"（busy_score={activity.busy_score:.2f}）",
        )
    if len(lines) == 1:
        lines.append("- 你目前沒有特別在做什麼，正好可以履行這個承諾。")
    return lines


def _normalize(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _FENCE_RE.sub("", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > _MAX_REPLY_CHARS:
        text = text[:_MAX_REPLY_CHARS].rstrip()
        last_break = max(
            text.rfind("。"),
            text.rfind("！"),
            text.rfind("？"),
            text.rfind("\n"),
        )
        if last_break > _MAX_REPLY_CHARS * 0.6:
            text = text[: last_break + 1].rstrip()
    return text
