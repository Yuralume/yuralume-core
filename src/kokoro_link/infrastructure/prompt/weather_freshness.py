"""Shared prompt fragment: the live weather fact + its freshness authority.

Chat, the proactive decider and the proactive intention judge all read
the *same* weather string (produced once per turn by the weather facts
renderer), so the wrapper around it lives here to stop the surfaces from
drifting — chat saying "外面放晴了" while the proactive push still hands
out umbrellas.

Why the directive exists at all: the weather fact is fresh every turn,
but everything else the model reads — dialogue history, memories, feed
posts, and above all the day's schedule text, which was planned hours
earlier — can still be soaked in this morning's rain. Without being told
which layer wins, the model keeps echoing the stale one.

LLM-first stance: this is a **precedence** directive, not a
behavioural one. It says which fact to trust; it never tells the
character how to react to the weather. Reaction stays persona-driven.
"""

from __future__ import annotations

# Kept verbatim from the chat prompt builder's original inline wording —
# chat has been shipping it since the 6/30 fix, and rewording it would
# silently re-tune a prompt that already behaves.
_FRESHNESS_AUTHORITY = (
    "（以上為此刻真實天氣事實層。若近期對話、記憶、貼文或行程描述隱含的天氣"
    "與此刻不一致——例如先前在下雨、現在已轉晴——一律以此刻天氣事實為準，"
    "不要延續已經過時的天氣狀態或提醒。）"
)


def render_weather_fact_lines(
    weather_context: str,
    *,
    max_fact_chars: int | None = None,
) -> list[str]:
    """Return the weather fact followed by the authority directive, or ``[]``.

    Empty / whitespace input renders nothing so callers can splice
    unconditionally. ``max_fact_chars`` clamps the *fact* only (proactive
    prompts bound it to keep the prompt small); the directive is never
    truncated away, otherwise the clamp would quietly undo the fix.
    """
    weather = weather_context.strip()
    if not weather:
        return []
    if max_fact_chars is not None:
        weather = weather[:max_fact_chars]
    return [weather, _FRESHNESS_AUTHORITY]
