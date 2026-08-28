"""Behavioural pins for the TR3 health-concern section.

The golden corpus already proves *what the block says, byte for byte*.
These prove the two things a byte-oracle cannot: that both the positive
move and every prohibition survive edits (FB rule 7 — a section of pure
prohibitions teaches over-avoidance), and that the block sits where it
is supposed to in the cacheable prefix.
"""

from __future__ import annotations

from kokoro_link.infrastructure.prompt.sections.health_care import SECTIONS
from kokoro_link.infrastructure.prompt.sections.order import SECTION_ORDER

HEALTH_CARE_HEADING = "健康關懷界線"
DOCTOR_NUDGE = "去看一下醫生吧"


def _render() -> list[str]:
    (entry,) = SECTIONS
    return entry.render(None)  # type: ignore[arg-type]


def _text() -> str:
    return "\n".join(_render())


# --- the baseline is unconditional and constant -----------------------


def test_the_block_renders_with_no_context_at_all() -> None:
    # Unlike honesty_discipline, nothing here branches on a per-turn or
    # per-character fact — passing ``None`` as the context is the point.
    assert HEALTH_CARE_HEADING in _text()


def test_the_block_is_deterministic() -> None:
    assert _render() == _render()


# --- FB rule 7: positive and negative paired, not prohibitions alone --


def test_the_positive_move_is_present_for_both_ends_of_the_spectrum() -> None:
    body = _text()
    assert DOCTOR_NUDGE in body
    assert "才不是擔心你" in body  # prickly/tsundere can still say it
    assert "我真的有點擔心" in body  # gentle can still say it
    assert body.count("✅") >= 2


def test_every_prohibition_names_the_failure_it_forbids() -> None:
    body = _text()
    assert "衛教口吻" in body
    assert "條列式問診" in body
    assert "不要診斷" in body
    assert "求助專線" in body
    assert body.count("❌") >= 4


def test_the_trigger_is_scoped_away_from_throwaway_small_talk() -> None:
    # The opposite guard: not every body-adjacent word should fire this.
    body = _text()
    assert "有點累" in body
    assert "不需要特別接關懷" in body


# --- placement ----------------------------------------------------------


def test_the_section_sits_right_after_honesty_in_the_cacheable_prefix() -> None:
    order = list(SECTION_ORDER)
    assert order[order.index("honesty_discipline") + 1] == "health_care"
    assert order.index("health_care") < order.index("presence_frame")
