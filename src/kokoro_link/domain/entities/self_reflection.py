"""Self-narrative reflection entity (HUMANIZATION_ROADMAP §3.2).

The dream pass periodically asks the LLM to read the character's
high-salience memories + recent emotion events over a 7- or 30-day
window and write a short first-person narrative — "我這週過得怎麼樣" /
"這個月有什麼留在我心裡的事". The narrative becomes a fact-layer prompt
block that chat / proactive paths inject as inner motivation context.

Design notes:

- **Per-(character, operator)**: a reflection narrates the character's
  *relationship-bound* inner life from one operator's perspective. Two
  operators each get their own reflection rows so cross-character
  isolation holds.
- **Evidence quote required**: when the reflection references user
  memory, ``evidence_quote`` must be a verbatim snippet (same anti-
  hallucination guard ``OperatorPersona`` extraction uses). The
  extractor's prompt enforces it.
- **L3 OK to inject — system prompt rail required**: per §5
  2026-05-21 decision the reflection may include Layer-3 sensitive
  material (the character genuinely *should* remember when the user
  was vulnerable). The chat / proactive system prompt MUST pin the
  最高原則 "禁止情勒、禁止拿傷疤開玩笑" or this entity becomes a
  weaponisable mirror. Enforcement lives in the prompt builder, not
  here — the entity stores facts.
- **TTL**: a fresh reflection supersedes older ones for the same
  (character, operator, period); the dream pass removes / archives
  stale rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Final
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


PERIOD_WEEK: Final = "week"
PERIOD_MONTH: Final = "month"

_VALID_PERIODS: Final = frozenset({PERIOD_WEEK, PERIOD_MONTH})


@dataclass(frozen=True, slots=True)
class SelfReflection:
    id: str
    character_id: str
    operator_id: str
    period: str
    """``week`` (7-day window) or ``month`` (30-day window). Two rows
    can coexist per pair — one short-window pulse, one long-window
    summary — and the prompt builder surfaces both."""
    narrative: str
    """First-person Chinese narrative; ≤ ~400 chars. Must read like the
    character's own inner monologue, not an outside report."""
    dominant_themes: tuple[str, ...]
    """≤ 5 short tags (work / relationships / health / creative / ...).
    Used by the prompt builder for ordering and by future filters."""
    period_start: date
    period_end: date
    evidence_quotes: tuple[str, ...] = field(default_factory=tuple)
    """Up to 3 verbatim snippets from memory rows / messages cited by
    the reflection. Same anti-hallucination guard as persona extraction.
    Empty tuple is allowed when the LLM produced a purely state-level
    reflection that does not lean on specific quoted moments."""
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.period not in _VALID_PERIODS:
            raise ValueError(
                f"SelfReflection.period must be one of {sorted(_VALID_PERIODS)}, "
                f"got {self.period!r}",
            )
        if not self.character_id.strip():
            raise ValueError("SelfReflection.character_id must be non-empty")
        if not self.operator_id.strip():
            raise ValueError("SelfReflection.operator_id must be non-empty")
        if not self.narrative.strip():
            raise ValueError("SelfReflection.narrative must be non-empty")
        if self.period_end < self.period_start:
            raise ValueError(
                "SelfReflection.period_end must be >= period_start",
            )

    @classmethod
    def new(
        cls,
        *,
        character_id: str,
        operator_id: str,
        period: str,
        narrative: str,
        dominant_themes: tuple[str, ...] | list[str] = (),
        period_start: date,
        period_end: date,
        evidence_quotes: tuple[str, ...] | list[str] = (),
        now: datetime | None = None,
    ) -> "SelfReflection":
        themes = tuple(
            (t or "").strip() for t in dominant_themes if (t or "").strip()
        )[:5]
        quotes = tuple(
            (q or "").strip() for q in evidence_quotes if (q or "").strip()
        )[:3]
        return cls(
            id=str(uuid4()),
            character_id=character_id.strip(),
            operator_id=operator_id.strip(),
            period=period,
            narrative=narrative.strip(),
            dominant_themes=themes,
            period_start=period_start,
            period_end=period_end,
            evidence_quotes=quotes,
            created_at=now or _utcnow(),
        )
