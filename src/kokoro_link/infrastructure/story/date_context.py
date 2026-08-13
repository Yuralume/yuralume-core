"""Absolute-date context shared by every story-arc generation prompt.

Why this module exists (
CF1b): an arc premise, a beat title and a beat summary are written once
and then re-injected into chat / proactive prompts every day until the
beat is realized — sometimes for weeks. A beat summary that froze the
word 「明天」 at authoring time reads as "tomorrow" *again* on every
later injection, which is one of the sources of the zombie-appointment
loop (the character keeps promising a meet-up that was already missed).

The fix is LLM-first: never rewrite the model's prose afterwards (that
would be keyword surgery on player-visible text, banned by the repo's
top directive). Instead give every story writer the same two things —
the arithmetic it needs (this block) and the discipline stated in the
template (the 時間紀律 section of each ``data/prompts/story/*.txt``).

The anchor rows themselves come from
:func:`~kokoro_link.infrastructure.prompt.timing_utils.render_date_anchor_lines_for_day`
so story prompts and the post-turn / schedule / goal writers all show
the model the same table.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_weekday_label,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    render_date_anchor_lines_for_day,
)

_NO_DATE_CONTEXT = (
    "（本次沒有可靠的今天日期；因此敘述中不要出現「今天／明天／後天／昨天／"
    "前天／這週末」這類相對時間詞，改用不會過期的說法。）"
)


def format_absolute_day(day: date, language_tag: str | None = None) -> str:
    """Render one civil day as ``YYYY-MM-DD（星期X）``.

    Weekday localisation follows the operator language so the model can
    copy the annotation straight into player-visible prose written in
    that language.
    """
    return f"{day.isoformat()}（{localized_weekday_label(day.weekday(), language_tag)}）"


def render_story_date_context_block(
    today: date | None,
    *,
    language_tag: str | None = None,
    include_today_line: bool = True,
    extra_lines: Sequence[str] = (),
) -> str:
    """Build the date block spliced into a story prompt as one string.

    ``include_today_line`` is ``False`` for templates that already print
    「今天：${today}」 themselves — the anchor table then supplies the
    weekday and the surrounding days without duplicating the heading.

    ``extra_lines`` carries prompt-specific coordinates (the arc's start
    and end date, for instance) that belong in the same block.

    Returns a self-contained multi-line string; when ``today`` is unknown
    the block degrades to an explicit "no date context, so avoid relative
    time words" instruction rather than silently dropping the discipline.
    """
    if today is None:
        return _NO_DATE_CONTEXT
    lines: list[str] = []
    if include_today_line:
        lines.append(f"今天：{format_absolute_day(today, language_tag)}")
    lines.extend(extra_lines)
    lines.extend(
        render_date_anchor_lines_for_day(today, language_tag=language_tag),
    )
    return "\n".join(lines)
