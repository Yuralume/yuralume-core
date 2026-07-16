"""Shared policy constants for the busy-defer rhythm.

These thresholds are structural gates around the LLM decision, not
semantic substitutes for it. The LLM still decides whether a high-cost
activity should defer; the floor only avoids calls in clearly reachable
states and keeps defer release symmetric.
"""

BUSY_REPLY_DECIDER_INVOKE_FLOOR = 0.7
"""Minimum current ``busy_score`` before chat asks the busy decider."""

BUSY_FOLLOW_UP_RELEASE_CEILING = BUSY_REPLY_DECIDER_INVOKE_FLOOR
"""Rows stay queued while current ``busy_score`` is at or above this."""

