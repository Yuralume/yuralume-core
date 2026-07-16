"""Busy-reply decider port.

When a user message arrives and the character is in the middle of a
high ``busy_score`` activity, the chat path asks this port to decide:

* **Reply immediately anyway** — sometimes the message is urgent or
  short enough that breaking off mid-meeting is the natural move
  ("快遲到了求救！"). The character's personality + the message
  content informs this — short hardcoded thresholds can't tell
  "晚餐吃什麼" apart from "我跌倒了快來".
* **Defer with a brief acknowledgement** — write a short, in-character
  "I'm busy with X, I'll get back to you when Y" line that the user
  sees immediately, queue the actual reply for later. ``defer_until``
  hints at when the dispatcher should try to release the queued row;
  the actual release is double-gated on the current busy_score.

Per the project's top directive (``CLAUDE.md``), the judgement is the
LLM's. The decider port enumerates **nothing** — no busy-score
threshold, no category list, no keyword catalogue. The implementation
LLM sees the persona, the current activity, the message, and writes
its own call.

Empty / fail-soft output means "let the normal chat path handle it"
(treat as ``immediate``). Same shape as ``IdleDriftPort`` /
``ActivityAftermathPort``: text in, structured-but-tolerant text out,
caller never receives an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import ClassVar, Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import ScheduleActivity


@dataclass(frozen=True, slots=True)
class BusyReplyMode:
    """Two-valued VO instead of an enum — keeps JSON serialisation /
    pattern-matching trivial and lets us extend later (e.g. "delegate"
    if we ever add a private-NPC stand-in) without a domain change."""

    value: str

    IMMEDIATE: ClassVar["BusyReplyMode"]
    BRIEF_DEFER: ClassVar["BusyReplyMode"]

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("BusyReplyMode value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value


BusyReplyMode.IMMEDIATE = BusyReplyMode("immediate")
BusyReplyMode.BRIEF_DEFER = BusyReplyMode("brief_defer")


@dataclass(frozen=True, slots=True)
class BusyDecision:
    """Decider verdict.

    ``mode`` drives the call site:
    * ``IMMEDIATE`` → chat path runs normally; other fields ignored.
    * ``BRIEF_DEFER`` → ``brief_reply`` becomes the assistant turn the
      user sees inline; a ``PendingFollowUp`` row is queued and
      ``defer_until`` becomes the row's ``scheduled_for``.

    Empty result (``mode == IMMEDIATE`` and everything else blank) is the
    correct fail-soft response — caller treats it as "no defer".
    """

    mode: BusyReplyMode = BusyReplyMode.IMMEDIATE
    brief_reply: str = ""
    """Short, in-character ack the user sees inline. Required when
    ``mode == BRIEF_DEFER``; ignored otherwise. The decider writes this
    in the character's voice ("先回會議結束我再好好回你") — it is **not**
    a template. The eventual deferred-reply LLM sees this string so the
    full follow-up can honour the promise."""

    defer_until: datetime | None = None
    """Earliest UTC instant the dispatcher should consider releasing
    the deferred reply. ``None`` defaults to the end of the current
    activity (caller derives). Always tz-aware; naive datetimes are
    treated as ``None``."""

    defer_reason: str = ""
    """Short label / phrase for telemetry + the eventual follow-up
    prompt context ("會議中" / "深度寫作"). Free-form."""

    @property
    def is_defer(self) -> bool:
        return self.mode == BusyReplyMode.BRIEF_DEFER and bool(
            self.brief_reply.strip()
        )


class BusyReplyDeciderPort(Protocol):
    async def decide(
        self,
        *,
        character: Character,
        user_message: str,
        current_activity: ScheduleActivity | None,
        recent_dialogue_summary: str | None = None,
        recent_proactive_attempts: tuple[ProactiveAttempt, ...] = (),
        relationship_context_lines: tuple[str, ...] = (),
        interaction_context_lines: tuple[str, ...] = (),
        now: datetime,
        local_tz: tzinfo | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> BusyDecision:
        """Judge whether to defer the reply.

        ``operator_primary_language`` (BCP 47) is the content language for
        the player-visible ``brief_reply`` ack. Without it a non-Chinese
        player messaging a busy character receives a Traditional-Chinese
        acknowledgement while the deferred follow-up composer (already
        language-aware) replies in the right language — the mismatch makes
        the Chinese ack stand out. Defaults to ``zh-TW`` (ship-first) so
        legacy callers keep working.

        ``relationship_context_lines`` and ``interaction_context_lines``
        are already prompt-shaped qualitative facts. The application
        layer resolves initial relationship seed and Layer-4 familiarity;
        the adapter renders them without exposing raw scores or message
        counts to the LLM.

        ``recent_proactive_attempts`` are the character's own most-recent
        SENT proactive pushes (newest first). They let the decider tell
        "the user is replying to outreach I just initiated" apart from
        "an unsolicited message interrupting my focus" — the two read as
        opposite framings of the same busy activity. A character that
        just chose to reach out should not normally turn around and defer
        the reply with the busy mechanism; surfacing the fact (with
        elapsed time) lets the LLM weigh it per persona rather than via a
        hardcoded rule. Empty tuple means "no recent outreach context".

        Implementations must be fail-soft: any internal error
        (model timeout, parse fail, empty response) returns a default
        :class:`BusyDecision` (``IMMEDIATE``). The chat path must
        always have a usable answer so a flaky decider can't lock the
        user out of getting a reply.
        """
