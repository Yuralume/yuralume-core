"""Scheduled-promise composer port.

Sibling of :class:`PendingFollowUpComposerPort` but for the
``scheduled_promise`` variant of :class:`PendingFollowUp`: the user
explicitly asked the character to message them at a specific future
time ("明天 10 點叫我起床" / "中午記得提醒我吃飯") and the post-turn
extractor lodged a queued row. At the promised time the dispatcher
calls this composer to write the actual outbound message.

Unlike the busy-defer composer, there's no inline brief_reply to honour
and no queued user-messages backlog to wrap up — the message is
generated fresh from persona + promise_intent + current schedule
context. Output is a single string; empty = retry next tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.content_flow import CONTENT_TOLERANCE_FRONTIER


@dataclass(frozen=True, slots=True)
class ScheduledPromiseComposeInput:
    character: Character
    promise_intent: str
    """What the character promised to do at this time (post-turn LLM
    output, e.g.「叫使用者起床」「提醒使用者吃午餐」). The composer
    interprets the intent through the character's persona — the same
    intent written by 古板嚴肅 vs 軟糯撒嬌 should read very differently.
    """
    promise_text: str
    """The original user-side wording that produced the promise (例:
    「明天 10 點叫我起床嘛」). Optional context for callback flavour;
    empty when the source turn wasn't captured."""
    scheduled_for: datetime
    """The promised moment. Almost always close to ``now`` since the
    dispatcher releases when ``scheduled_for <= now``, but small skews
    can occur if a previous tick failed and the row retried."""
    current_activity: ScheduleActivity | None
    """Whatever the character is doing *now*. ``None`` = free time.
    Lets the composer write believable transitions ("剛從健身房回來，
    時間到了 — 早安")."""
    just_finished_activity: ScheduleActivity | None
    recent_dialogue_summary: str | None
    now: datetime
    operator_persona_lines: tuple[str, ...] = ()
    """Prompt-ready lines from OperatorPersonaService for this
    character/operator pair. Empty when persona is disabled or not yet
    learned."""
    operator_primary_language: str = "zh-TW"
    """BCP 47 tag of the character owner's pinned content language
    (FRONTEND_I18N_PLAN). The promised callback uses the same language
    as chat / proactive / busy-defer so a single conversation thread
    can't switch languages mid-arc."""
    local_tz: tzinfo = timezone.utc
    """User timezone for rendering the promised civil time."""
    promise_content_mode: MessageContentMode = MessageContentMode.NORMAL
    """Write-time mode for ``promise_text``. Frontier prompts must not
    receive raw text captured during NSFW mode."""
    promise_safe_summary: str = ""
    """Frontier-safe replacement for ``promise_text`` when available."""
    content_tolerance: str = CONTENT_TOLERANCE_FRONTIER
    """Prompt content-flow tolerance for this compose call."""


@dataclass(frozen=True, slots=True)
class ScheduledPromiseComposeOutput:
    content_text: str
    """The full outbound message. Empty string = no usable output —
    the dispatcher leaves the pending row in ``queued`` so the next
    tick retries (same fail-soft policy as the busy-defer composer)."""


class ScheduledPromiseComposerPort(Protocol):
    async def compose(
        self, payload: ScheduledPromiseComposeInput,
    ) -> ScheduledPromiseComposeOutput:
        """Write the promised outbound message. Must be fail-soft —
        any internal error (model timeout, parse fail, empty output)
        returns :class:`ScheduledPromiseComposeOutput` with an empty
        ``content_text`` rather than raising. The dispatcher treats an
        empty body as "retry next tick"."""
