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
from kokoro_link.infrastructure.prompt.outcome_claim_honesty import (
    append_honesty_correction,
)
from kokoro_link.infrastructure.prompt.player_knowledge_lines import (
    render_schedule_activity_knowledge_line,
)
from kokoro_link.infrastructure.prompt.player_persona_note_lines import (
    render_player_persona_note_lines,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_civil_days_ago_label,
    format_gap_duration_label,
    format_relative_past_label,
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
    # Same assembly as the busy-defer composer: declaration first, then
    # the inferred portrait, into the one existing template slot.
    operator_block_lines = [
        *render_player_persona_note_lines(payload.player_persona_note),
        *_operator_persona_block(payload.operator_persona_lines),
    ]
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
    promise_made_block = _promise_made_block(payload)
    tools_block = _tools_block(payload)
    body = get_default_loader().render(
        "busy/scheduled_promise_composer",
        promise_intent=payload.promise_intent.strip()[:300],
        scheduled_at_local=scheduled_local.strftime("%Y-%m-%d %H:%M"),
        scheduled_freshness=_scheduled_freshness(payload),
        original_block=original_block,
        promise_made_block=promise_made_block,
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
    # HV1: only ever non-empty on a re-compose the honesty gate ordered.
    # Appended in code rather than added as a template placeholder — the
    # shipped template describes the ordinary round, and a slot that is
    # blank on every call but a retry would have to be kept in sync across
    # the baseline pack and every tuned overlay for no benefit.
    return append_honesty_correction(body, payload.honesty_correction)


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


_OVERDUE_FLOOR_MINUTES = 10.0
"""Below this the release is punctual for conversational purposes.

Not a staleness policy — the model still decides what to do about a late
promise. This only picks which *fact* is true, and it is set by the
dispatcher's own cadence: the tick runs about every five minutes, so a
promise due at 07:00 is normally released by 07:05. A floor under that
would have characters apologising for ordinary scheduling jitter; a
floor much above it would swallow the 900-second quality / honesty
parks, which are exactly the delays worth admitting to."""


def _scheduled_freshness(payload: ScheduledPromiseComposeInput) -> str:
    """Say whether the promised moment just arrived, or arrived long ago.

    The template used to hard-code 「（剛到）」 next to the promised time.
    That string is a lie on every late release, and this path has four
    ways to be late — honesty park (+300s), judge outage (+900s), quality
    park (+900s), and any tick outage or leader handover — after which
    the model was shown 「約定時間：2026-06-19 10:00（剛到）」 directly
    above a 現在時間 line reading two days later. Two contradictory facts,
    and nothing telling it which to believe; a promise released a day
    late was written as if the alarm had just gone off.
    """
    late_minutes = (
        payload.now - payload.scheduled_for
    ).total_seconds() / 60.0
    if late_minutes < _OVERDUE_FLOOR_MINUTES:
        return "（剛到）"
    return f"（已經過了 {format_gap_duration_label(late_minutes)}，你晚了）"


def _promise_made_block(payload: ScheduledPromiseComposeInput) -> str:
    """Render the "when was this promise made" line, or nothing.

    ``SP1``: without a timing anchor the template's "你之前答應的事"
    section had no fact to attach to, so the model guessed — a promise
    made half an hour ago could come out as "昨天". ``promise_made_at``
    is optional (rows written before this field existed carry ``None``),
    so this returns an empty string in that case and the ordinary
    template block renders exactly as before — fail-soft, never a
    KeyError from a missing substitution.
    """
    label = _promise_made_ago_label(payload)
    if not label:
        return ""
    return f"\n- 這個承諾是對方在 {label} 向你提的"


def _promise_made_ago_label(payload: ScheduledPromiseComposeInput) -> str:
    """Combine a duration anchor with a civil-day anchor.

    Duration alone (``約 7 小時前``) is what most promises need. But a
    promise made late at night and fulfilled the next morning is still
    "only a few hours ago" by duration while already being "昨天" by the
    civil calendar the character and player both live in ("昨晚說的明早
    叫我" must not read as same-day) — so the civil-day tag rides along
    whenever it disagrees with "today".
    """
    if payload.promise_made_at is None:
        return ""
    minutes = max(
        0.0, (payload.now - payload.promise_made_at).total_seconds() / 60.0,
    )
    relative = format_relative_past_label(minutes)
    civil = format_civil_days_ago_label(
        payload.promise_made_at, payload.now, local_tz=payload.local_tz,
    )
    if civil and civil != "今天":
        return f"{relative}（{civil}）"
    return relative


def _schedule_block(payload: ScheduledPromiseComposeInput) -> list[str]:
    lines = ["活動脈絡："]
    lines.extend(
        render_current_time_fact_lines(payload.now, payload.local_tz, heading=None),
    )
    has_activity = False
    if payload.just_finished_activity is not None:
        activity = payload.just_finished_activity
        loc = f"（{activity.location}）" if activity.location else ""
        lines.append(
            f"- 剛結束：{activity.category} — {activity.description}{loc}",
        )
        has_activity = True
    if payload.current_activity is not None:
        activity = payload.current_activity
        loc = f"（{activity.location}）" if activity.location else ""
        lines.append(
            f"- 現在進行：{activity.category} — {activity.description}{loc}"
            f"（busy_score={activity.busy_score:.2f}）",
        )
        has_activity = True
    if not has_activity:
        lines.append("- 你目前沒有特別在做什麼，正好可以履行這個承諾。")
    else:
        # KB9: an activity description can name a companion or place from
        # material the player never read (see player_knowledge_lines).
        lines.append(render_schedule_activity_knowledge_line())
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
