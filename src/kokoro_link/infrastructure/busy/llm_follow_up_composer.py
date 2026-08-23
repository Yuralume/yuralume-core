"""LLM-backed adapter for the deferred-reply composer port.

Builds a Chinese prompt that lays out the character's persona, the
queued user messages, the brief in-character ack the user already saw,
and the activity context around the deferral, then asks the model to
write the actual reply.

Output is plain prose. Light normalisation: strip optional code-fence
wrappers, collapse repeated whitespace, cap length so a runaway model
can't post a novel. Empty output (after trim) → fail-soft empty so the
dispatcher leaves the row queued for retry.

**Tool passes (PF2).** Same contract as
:class:`LLMScheduledPromiseComposer` — read that adapter first, this
one mirrors its three seams. When the caller offers ``available_tools``
(a queued message asked the character to look something up or do
something while it was busy), the prompt gains the shared tools block
and the model may answer with a JSON tool call instead of prose; we
parse it and return it as ``tool_calls`` for the application layer to
execute via :mod:`kokoro_link.application.services.composer_tool_loop`
— this adapter never runs a tool itself. On the second pass the caller
supplies ``tool_results`` (successes *and* failures) and no tools, so
the model writes the follow-up reply from what it actually got. Any
JSON emitted when no tools are on offer is suppressed rather than
shipped: a player must never receive a raw ``{"tool": …}`` blob as
their follow-up reply.

**Machine output is never the reply — on any pass**, at the width that
pass has earned; see the sibling adapter's module docstring for why the
guard hangs off "about to become player-visible text" rather than off
"this pass offered tools".
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
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
    PendingFollowUpComposeOutput,
    PendingFollowUpComposerPort,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_COMMUNITY,
    CONTENT_TOLERANCE_FRONTIER,
    content_tolerance_for_llm_provider,
    normalize_content_tolerance,
    requires_community_routing_for_unreplaceable_nsfw,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.player_persona_note_lines import (
    render_player_persona_note_lines,
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

_MAX_REPLY_CHARS = 800
"""Cap on the rendered reply. Catches the case where the model misreads
the schema and writes an essay. 800 chars is roughly the longest
believable IM reply — anything past gets trimmed rather than rejected so
we don't lose a coherent draft."""

_MAX_QUEUED_PER_LINE = 240
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class LLMPendingFollowUpComposer(PendingFollowUpComposerPort):
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
        self, payload: PendingFollowUpComposeInput,
    ) -> PendingFollowUpComposeOutput:
        if not payload.queued_messages:
            return PendingFollowUpComposeOutput(content_text="")
        routing_tolerance = _routing_tolerance_for_payload(
            payload,
            supports_routing=self._resolver.supports_content_tolerance_routing,
        )
        try:
            if await self._resolver.is_fake(
                character=payload.character,
                content_tolerance=routing_tolerance,
            ):
                return PendingFollowUpComposeOutput(content_text="")
            model, model_id = await self._resolver.resolve(
                character=payload.character,
                content_tolerance=routing_tolerance,
            )
        except Exception:
            _LOGGER.exception(
                "pending follow-up composer route resolve failed character=%s",
                payload.character.id,
            )
            return PendingFollowUpComposeOutput(content_text="")
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
                "pending follow-up composer LLM call failed character=%s",
                payload.character.id,
            )
            return PendingFollowUpComposeOutput(content_text="")
        return _output_for(raw, payload)


def _output_for(
    raw: str, payload: PendingFollowUpComposeInput,
) -> PendingFollowUpComposeOutput:
    """Split the model's raw answer into "a tool call" or "the reply".

    Parsing runs against ``raw`` rather than the normalised text because
    normalisation strips fences and truncates at ``_MAX_REPLY_CHARS`` —
    either can mangle a JSON call before we ever look at it. Mirrors
    ``llm_scheduled_promise_composer._output_for`` exactly.
    """
    allowed = {tool.name for tool in payload.available_tools}
    if allowed:
        call = parse_tool_call(raw)
        if call is not None and call.name in allowed:
            return PendingFollowUpComposeOutput(
                content_text="", tool_calls=(call,),
            )
        if call is not None:
            _LOGGER.info(
                "pending follow-up composer asked for unavailable tool %r "
                "character=%s — treating as no output",
                call.name, payload.character.id,
            )
            return PendingFollowUpComposeOutput(content_text="")
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
            "pending follow-up composer returned machine output instead of a "
            "reply character=%s tools_offered=%d — suppressing",
            payload.character.id, len(allowed),
        )
        return PendingFollowUpComposeOutput(content_text="")
    if looks_like_tool_call_attempt(raw):
        # Either a malformed call, or a call on the pass where no tools
        # were offered. Shipping the blob would put raw JSON in the
        # player's chat; empty text means "retry next tick" instead.
        _LOGGER.warning(
            "pending follow-up composer emitted an unusable tool call "
            "character=%s tools_offered=%d — suppressing",
            payload.character.id, len(allowed),
        )
        return PendingFollowUpComposeOutput(content_text="")
    return PendingFollowUpComposeOutput(content_text=_normalize(raw))


class NullPendingFollowUpComposer(PendingFollowUpComposerPort):
    """Always emits empty output — used by the fake-provider path."""

    async def compose(
        self, payload: PendingFollowUpComposeInput,
    ) -> PendingFollowUpComposeOutput:
        return PendingFollowUpComposeOutput(content_text="")


# ----------------------------------------------------------------------
# Prompt construction
# ----------------------------------------------------------------------


def _build_prompt(payload: PendingFollowUpComposeInput) -> str:
    character = payload.character
    persona = "\n".join(_persona_block(character))
    # The declaration leads and the inferred portrait follows, in the one
    # template slot that already carries "what we know about the human".
    # Assembled here rather than as a second placeholder because the
    # shipped prompt template is not this seam's to change.
    operator_block_lines = [
        *render_player_persona_note_lines(payload.player_persona_note),
        *_operator_persona_block(payload.operator_persona_lines),
    ]
    operator_block = "\n" + "\n".join(operator_block_lines) if operator_block_lines else ""
    queued_block = "\n".join(_queued_messages_block(payload))
    schedule_block = "\n".join(_schedule_block(payload))
    summary = (payload.recent_dialogue_summary or "").strip()
    summary_block = "\n\n最近對話脈絡：\n" + summary[:400] if summary else ""
    elapsed = (payload.now - payload.queued_at).total_seconds() / 60.0
    tools_block = _tools_block(payload)
    body = get_default_loader().render(
        "busy/follow_up_composer",
        persona_block=persona,
        operator_persona_block=operator_block,
        schedule_block=schedule_block,
        brief_reply=payload.brief_reply.strip()[:160],
        defer_reason=payload.defer_reason or "（未說明，自行依活動判斷）",
        elapsed_text=_humanize_minutes(elapsed),
        summary_block=summary_block,
        queued_block=queued_block,
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


def _tools_block(payload: PendingFollowUpComposeInput) -> str:
    """The shared chat/proactive tools section, first pass only.

    Empty on every pre-PF2 call and on the second pass — the tool has
    already run by then, and re-offering it invites a second call the
    loop would not execute."""
    if not payload.available_tools:
        return ""
    lines = render_tools_block(list(payload.available_tools))
    return "\n\n" + "\n".join(lines) if lines else ""


def _tool_results_block(payload: PendingFollowUpComposeInput) -> str:
    if not payload.tool_results:
        return ""
    lines = render_tool_outcomes_block(list(payload.tool_results))
    return "\n\n" + "\n".join(lines) if lines else ""


def _routing_tolerance_for_payload(
    payload: PendingFollowUpComposeInput,
    *,
    supports_routing: bool,
) -> str | None:
    if not supports_routing:
        return None
    if requires_community_routing_for_unreplaceable_nsfw(payload.queued_messages):
        return CONTENT_TOLERANCE_COMMUNITY
    return None


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
        "- 使用這些資訊時要像自然熟人一樣克制；不確定就問，不要裝熟或背誦資料。",
    ]


def _schedule_block(payload: PendingFollowUpComposeInput) -> list[str]:
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
        lines.append("- 你目前剛好有空，可以好好回覆對方。")
    return lines


def _queued_messages_block(payload: PendingFollowUpComposeInput) -> list[str]:
    lines: list[str] = []
    for index, message in enumerate(payload.queued_messages, start=1):
        text = _queued_message_text(
            message.content,
            content_mode=message.content_mode,
            safe_summary=message.safe_summary,
            content_tolerance=payload.content_tolerance,
        )
        if not text:
            continue
        if len(text) > _MAX_QUEUED_PER_LINE:
            text = text[:_MAX_QUEUED_PER_LINE].rstrip() + "…"
        lines.append(f"{index}. {text}")
    if not lines:
        lines.append(
            "（對方的訊息內容在目前模型容忍度下不可直接提供；"
            "請不要猜測露骨細節，只自然表達你回來了，並請對方重新開啟適合的模式後再續談。）",
        )
    return lines


def _queued_message_text(
    content: str,
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
    return (content or "").strip()


def _should_use_safe_summary(
    content_mode: MessageContentMode,
    *,
    content_tolerance: str,
) -> bool:
    return (
        normalize_content_tolerance(content_tolerance) == CONTENT_TOLERANCE_FRONTIER
        and content_mode is MessageContentMode.NSFW
    )


def _humanize_minutes(minutes: float) -> str:
    if minutes < 1:
        return "不到 1 分鐘"
    if minutes < 60:
        return f"約 {int(minutes)} 分鐘"
    hours = minutes / 60.0
    if hours < 24:
        return f"約 {hours:.1f} 小時"
    days = hours / 24.0
    return f"約 {days:.1f} 天"


# ----------------------------------------------------------------------
# Output normalisation
# ----------------------------------------------------------------------


def _normalize(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _FENCE_RE.sub("", text).strip()
    # Collapse 3+ blank lines down to a single one so multi-paragraph
    # outputs still flow naturally without leaving wide gaps in the
    # chat UI.
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > _MAX_REPLY_CHARS:
        text = text[:_MAX_REPLY_CHARS].rstrip()
        # Best-effort sentence cut so we don't truncate mid-clause.
        last_break = max(
            text.rfind("。"),
            text.rfind("！"),
            text.rfind("？"),
            text.rfind("\n"),
        )
        if last_break > _MAX_REPLY_CHARS * 0.6:
            text = text[: last_break + 1].rstrip()
    return text
