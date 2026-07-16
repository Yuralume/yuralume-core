"""No-op :class:`BusyReplyDeciderPort` — always answers ``IMMEDIATE``.

Wired when the deployment is on the fake provider so the chat path
degrades to its pre-defer behaviour without a feature flag dotted
throughout the application.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from kokoro_link.contracts.busy_reply_decider import (
    BusyDecision,
    BusyReplyDeciderPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import ScheduleActivity


class NullBusyReplyDecider(BusyReplyDeciderPort):
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
        _ = (
            local_tz,
            recent_proactive_attempts,
            relationship_context_lines,
            interaction_context_lines,
            operator_primary_language,
        )
        return BusyDecision()
