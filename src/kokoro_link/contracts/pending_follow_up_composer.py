"""Deferred-reply composer port.

Runs at the proactive-scheduler tick when a queued ``PendingFollowUp``
row's release conditions are met: the character is no longer high-busy
and the scheduled time has arrived. The composer reads the queued user
messages + the brief in-character ack the user already saw + chat
context, and writes the actual full reply.

Output is a single string. The dispatcher fans it out through the same
web SSE + Telegram/LINE delivery path proactive messages use, and
writes a ``proactive_attempt`` row with ``trigger=PENDING_FOLLOW_UP``
for telemetry / cooldown tracking (the gate bypasses cooldown for this
trigger).

Per the project's top directive, the composer reads the persona + the
queued conversation and writes its own call — no templating, no
keyword catalogue. ``recent_dialogue_summary`` provides "what the
character has been talking about lately"; ``current_activity`` (after
the busy one ended) helps the model write a believable transition
("剛開完會") instead of an abrupt context-free continuation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUpMessage,
)
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.content_flow import CONTENT_TOLERANCE_FRONTIER


@dataclass(frozen=True, slots=True)
class PendingFollowUpComposeInput:
    character: Character
    queued_messages: tuple[PendingFollowUpMessage, ...]
    """The merged user messages the character owes a reply to, oldest
    first. May be a single entry or up to ``MAX_QUEUED_MESSAGES``."""
    brief_reply: str
    """The in-character ack the user saw inline when the deferral was
    decided. The composer should honour the promise this implies (e.g.
    if the ack said "等開完會我再仔細回", don't pretend the meeting
    didn't happen)."""
    defer_reason: str
    """Short label from the original decider ("會議中" / "深度寫作")."""
    queued_at: datetime
    """When the first queued message arrived — lets the composer mention
    elapsed time ("剛剛你問的那個", "拖了一個多小時，抱歉")."""
    just_finished_activity: ScheduleActivity | None
    """The activity that wrapped up just before the dispatcher decided
    to release. ``None`` when the dispatcher released purely on the
    schedule clock (no recently-completed activity)."""
    current_activity: ScheduleActivity | None
    """Whatever the character is doing *now* — may be a low-busy
    activity or ``None`` (free time)."""
    recent_dialogue_summary: str | None
    now: datetime
    local_tz: tzinfo = timezone.utc
    """Operator timezone for rendering ``now`` into prompt-visible civil time."""
    operator_persona_lines: tuple[str, ...] = ()
    """Prompt-ready lines from OperatorPersonaService for this
    character/operator pair. Empty when persona is disabled or not yet
    learned."""
    operator_primary_language: str = "zh-TW"
    """BCP 47 tag of the character owner's pinned content language
    (FRONTEND_I18N_PLAN). Lets the deferred reply land in the same
    language as live chat — without this a busy-defer follow-up could
    drift away from the brief ack the user already saw inline."""
    content_tolerance: str = CONTENT_TOLERANCE_FRONTIER
    """Prompt content-flow tolerance for this compose call.

    Frontier calls must not receive queued NSFW-mode raw text; community
    calls may keep the original queued text.
    """


@dataclass(frozen=True, slots=True)
class PendingFollowUpComposeOutput:
    content_text: str
    """The full reply text. Empty string means "no usable output" — the
    dispatcher leaves the pending row in ``queued`` state so a later
    tick can retry."""


class PendingFollowUpComposerPort(Protocol):
    async def compose(
        self, payload: PendingFollowUpComposeInput,
    ) -> PendingFollowUpComposeOutput:
        """Write the deferred reply text. Must be fail-soft — any
        internal error (model timeout, parse fail, empty response)
        returns :class:`PendingFollowUpComposeOutput` with an empty
        ``content_text`` rather than raising. The dispatcher treats an
        empty body as "retry next tick"."""
