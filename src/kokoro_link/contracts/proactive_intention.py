"""Port for the proactive pre-send intention judgement.

This judge sits after the cheap gate and before message composition. It
does not write the outbound message. Its job is to decide whether the
character has a strong enough inner motive to spend one of today's scarce
proactive slots right now.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.contracts.proactive import ProactiveContext


@dataclass(frozen=True, slots=True)
class ProactiveIntentionDecision:
    should_consume_slot: bool
    """Whether this evaluation should continue to message composition."""

    reason: str
    """Short operator-facing reason for the audit log."""

    inner_motive: str = ""
    conversation_purpose: str = ""
    expected_reply: str = ""
    risk: str = ""
    best_timing: str = ""


class ProactiveIntentionJudgePort(Protocol):
    async def judge(
        self, context: ProactiveContext,
    ) -> ProactiveIntentionDecision: ...
