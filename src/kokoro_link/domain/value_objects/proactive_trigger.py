"""Why a proactive evaluation was kicked off.

Keeping this as a string-valued VO (not an Enum) means new trigger
sources can be added without a domain-layer change.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class ProactiveTrigger:
    value: str

    TICK: "ClassVar[ProactiveTrigger]"
    POST_TURN: "ClassVar[ProactiveTrigger]"
    ACTIVITY_TRANSITION: "ClassVar[ProactiveTrigger]"
    ARC_BEAT: "ClassVar[ProactiveTrigger]"
    PENDING_FOLLOW_UP: "ClassVar[ProactiveTrigger]"
    SCHEDULED_PROMISE: "ClassVar[ProactiveTrigger]"

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("ProactiveTrigger value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "ProactiveTrigger":
        return cls(raw)


ProactiveTrigger.TICK = ProactiveTrigger("tick")
ProactiveTrigger.POST_TURN = ProactiveTrigger("post_turn")
ProactiveTrigger.ACTIVITY_TRANSITION = ProactiveTrigger("activity_transition")
# An active story-arc beat just realized on its scheduled date via the
# scheduler's tick-time check (Phase 3 of SCENE_BEAT_PLAN). Distinct
# from TICK so the dispatcher / decider can treat the freshly-landed
# beat as a stronger signal than "5 minutes passed".
ProactiveTrigger.ARC_BEAT = ProactiveTrigger("arc_beat")
# Releasing a queued ``PendingFollowUp``: the user previously sent a
# message during a high-busy_score window, got a brief in-character ack,
# and is now owed the actual reply. Distinct from TICK so the gate /
# dispatcher can bypass quiet-hours, daily-limit, and cooldown — this is
# a promise being fulfilled, not an unprompted push.
ProactiveTrigger.PENDING_FOLLOW_UP = ProactiveTrigger("pending_follow_up")
# Releasing a scheduled message promise: the user previously asked the
# character to message them at a specific future time (例:「明天 10 點叫我
# 起床」「中午記得提醒我吃飯」) and the post-turn extractor lodged a
# ``PendingFollowUp`` row with ``kind="scheduled_promise"``. Bypasses
# quiet_hours / daily_limit / cooldown / proactive_enabled gates — it's
# a promise being fulfilled, the user explicitly asked for this push.
ProactiveTrigger.SCHEDULED_PROMISE = ProactiveTrigger("scheduled_promise")
