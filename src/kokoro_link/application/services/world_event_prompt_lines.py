"""Prompt-line rendering for external-world (RSS) events.

Chat shows world events in two situations that look alike and mean
opposite things:

* **candidates** — events curated into this character's inbox that no
  surface has consumed yet (peeked, never claimed). "I came across
  this; I may or may not bring it up."
* **recall** — events the character already brought up with this player
  through some other surface (a proactive DM, and later whatever else
  writes a mention). "I told you about this; you may ask about it."

Both need the same fact set, so it is rendered once here rather than
re-derived at each call site.

**The link is the point.** A curated summary is a clipped RSS blurb; it
cannot answer "what else did it say". Without the URL in the prompt the
character has nothing to hand ``web_fetch``, so a follow-up question
about material it raised itself is unanswerable — the failure this
module exists to remove. The URL is a fact for the model to *fetch*,
not a string to read out: the prompt block that consumes these lines
says so.

Only ``http(s)`` links are rendered, and an absurdly long one is dropped
whole rather than clipped — a truncated URL cannot be fetched and would
invite the model to present a broken link as its source.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_datetime_ago_phrase,
)

TITLE_CLIP = 160
SOURCE_CLIP = 80
LOCALE_CLIP = 40
SUMMARY_CLIP = 240

MAX_URL_CHARS = 800
"""Longest URL rendered into the prompt.

Past this length the link is dropped, never shortened — a truncated URL
cannot be fetched and would invite the model to present a broken link as
its source.

The bound is generous on purpose. Bundled sources include Google News,
whose item links are base64-ish redirector URLs that routinely run past
300 characters; a tighter cap silently stripped the link from every
event of an entire shipped source, which is the exact failure this
module exists to prevent. Three recalled events at this worst case is
still small next to the rest of the prompt."""

_ALLOWED_URL_SCHEMES = ("https://", "http://")


def _clip(text: str, limit: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _fetchable_url(raw: str) -> str:
    """Return the URL only when it is safe and usable to hand a fetcher.

    Event URLs come from third-party feed content, so the scheme is not
    something to trust by construction."""
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL_CHARS:
        return ""
    lowered = url.lower()
    if not lowered.startswith(_ALLOWED_URL_SCHEMES):
        return ""
    if any(ch.isspace() for ch in url):
        return ""
    return url


def _event_facts(event: WorldEvent) -> str | None:
    """Render one event's facts, or ``None`` when it has no title.

    A title-less event is not worth a prompt line: the model would have
    nothing to name when raising it."""
    title = _clip(event.title or "", TITLE_CLIP)
    if not title:
        return None
    parts = [f"標題：{title}"]
    source = _clip(event.source or "", SOURCE_CLIP)
    if source:
        parts.append(f"來源：{source}")
    locale = _clip(event.locale or "", LOCALE_CLIP)
    if locale:
        parts.append(f"來源地區：{locale}")
    summary = _clip(event.summary or "", SUMMARY_CLIP)
    if summary:
        parts.append(f"摘要：{summary}")
    url = _fetchable_url(event.url or "")
    if url:
        parts.append(f"連結：{url}")
    return "；".join(parts)


def _ago_phrase(*, mentioned_at: datetime, now: datetime) -> str:
    """Coarse "how long ago" phrasing for a recalled mention.

    Deliberately coarse: the character is recalling that it said
    something, not quoting a timestamp. Thin wrapper around the shared
    :func:`~kokoro_link.infrastructure.prompt.timing_utils.format_datetime_ago_phrase`
    (SP2 timing-formatter consolidation) — kept local only to preserve
    this module's ``mentioned_at=``/``now=`` call-site naming."""
    return format_datetime_ago_phrase(past=mentioned_at, now=now)


def render_event_candidate_line(event: WorldEvent) -> str | None:
    """One line for an event the character has seen but not yet used."""
    return _event_facts(event)


def render_event_recall_line(
    event: WorldEvent, *, mentioned_at: datetime, now: datetime,
) -> str | None:
    """One line for an event the character already raised with the player."""
    facts = _event_facts(event)
    if facts is None:
        return None
    when = _ago_phrase(mentioned_at=mentioned_at, now=now)
    return f"（{when}你跟對方提過）{facts}"
