"""Shared prompt fragment: the consecutive-unanswered proactive streak.

Both the proactive decider and the intention judge surface the same
fact — "the character has pushed N times in a row without a reply" — so
the phrasing lives here to stop the two prompt paths from drifting (one
nudging the character to escalate while the other nudges retreat for
the very same streak).

Why this exists at all (the "跳針" bug): the per-message reply tag
("（對方還沒回）") only tells the model *that* the last push went
unanswered, and the old instructions then said "basically stay silent".
With no sense of *how many times in a row* it had been ignored — and no
licence to let that land emotionally — the character re-derived a
near-identical opener every day instead of evolving (mild interest →
worry → sulking → giving space), which reads as a broken record.

LLM-first stance (CLAUDE.md): this is a **fact layer**. It states the
count and opens the door to a persona-driven reaction. It must never
encode "N >= 3 → get angry"; direction and intensity are always the
model's call from persona + disposition + current state.
"""

from __future__ import annotations

# 1 unanswered push is already conveyed by the per-message
# "（對方還沒回）" reply tag, so we only add the dedicated streak block
# once it becomes a *run* worth reacting to as its own fact.
_STREAK_SURFACE_THRESHOLD = 2


def render_unanswered_streak_lines(streak: int) -> list[str]:
    """Lines describing a run of ignored proactive pushes, or ``[]``.

    Returns an empty list below the threshold so the prompt stays clean
    in the steady state. Callers concatenate with ``"\\n".join(...)``.
    """
    if streak < _STREAK_SURFACE_THRESHOLD:
        return []
    return [
        f"連續未獲回應（事實）：自對方上次發話以來，你已經連續主動傳了 "
        f"{streak} 則訊息，都還沒有得到任何回應。",
        "這不是要你再追問同一件事。重點是——「被晾著 / 已讀不回」這件事，"
        "在你心裡會不會留下什麼？你的態度與情緒會不會慢慢起變化（更在意、"
        "有點受傷、賭氣、識相地退一步給對方空間、或換一種完全不同的方式靠近）？"
        "方向與強度完全由你的性格、你們的關係與你當下的心情決定，"
        "就像真人面對一直沒回的訊息那樣自然演進。",
        "唯一的硬規則：不要用同樣的語氣、同樣的題材、同樣的問題再重來一次——"
        "若你選擇再開口，必須反映出這份『已經連續被冷落 N 次』之後的新心境，"
        "而不是把昨天的話換句話重講。",
    ]
