"""Behavioural pins for the HV2 honesty section.

The golden corpus already proves *what the block says*. These prove the
two things a byte-oracle cannot: that the baseline text survives the hop
that hides the tool rail, and that the browsing line treats an undeclared
capability set as unknown rather than absent.

The second one is the load-bearing case. Reading ``None`` as "no browsing"
would print 「你打不開網頁」 to every character on every surface that never
declared a capability set — including ones holding a live ``web_search``
— and the observable damage is silent: the model simply stops searching
and starts guessing, which is the exact failure this whole series exists
to stop, wearing the opposite costume.
"""

from __future__ import annotations

import pytest

from kokoro_link.infrastructure.prompt.sections.context import ToolsContext
from kokoro_link.infrastructure.prompt.sections.honesty import (
    BROWSING_TOOL_NAMES,
    SECTIONS,
    browsing_unavailable,
)
from kokoro_link.infrastructure.prompt.sections.order import SECTION_ORDER

HONESTY_HEADING = "誠實界線"
NO_BROWSING_MARK = "這個環境沒有給你上網的能力"


class _ToolsOnlyContext:
    """Enough context for this section, and deliberately nothing more."""

    def __init__(self, character_tool_names: tuple[str, ...] | None) -> None:
        self.tools = ToolsContext(
            available_tools=(),
            tool_outcomes=(),
            forced_tool_name=None,
            character_tool_names=character_tool_names,
        )


def _render(character_tool_names: tuple[str, ...] | None) -> list[str]:
    (entry,) = SECTIONS
    return entry.render(_ToolsOnlyContext(character_tool_names))


def _text(character_tool_names: tuple[str, ...] | None) -> str:
    return "\n".join(_render(character_tool_names))


# --- the baseline is unconditional -----------------------------------


@pytest.mark.parametrize(
    "names", [None, (), ("web_search",), ("generate_image", "web_fetch")],
)
def test_the_baseline_discipline_renders_whatever_the_capability_set_is(
    names: tuple[str, ...] | None,
) -> None:
    # The point of hoisting this out of ``tools_block``: no tool offered
    # on this hop must never mean no honesty rule on this hop.
    assert HONESTY_HEADING in _text(names)


def test_the_baseline_still_permits_fiction_and_future_promises() -> None:
    """FB rule 7: a section of pure prohibitions teaches over-avoidance."""
    body = _text(None)
    assert "我等等幫你看看" in body      # promising later is honest
    assert "我走過去把窗簾拉上" in body  # narrating the fiction is allowed
    assert body.count("✅") >= 3


def test_the_baseline_text_does_not_vary_with_the_offered_tools() -> None:
    """Constant bytes are what let this block sit in the cached prefix.

    Hop 0 offers the catalogue and hop 1 hides it; if this section's text
    moved between them it would break the DH5 stable prefix for every
    block behind it, on every tool-using turn.
    """
    assert _render(("web_search",)) == _render(("web_fetch", "web_search"))


# --- the browsing line is a positive claim, never an inference -------


def test_an_undeclared_capability_set_says_nothing_about_browsing() -> None:
    assert browsing_unavailable(None) is False
    assert NO_BROWSING_MARK not in _text(None)


def test_a_declared_empty_capability_set_is_a_positive_absence() -> None:
    assert browsing_unavailable(()) is True
    assert NO_BROWSING_MARK in _text(())


@pytest.mark.parametrize("tool_name", sorted(BROWSING_TOOL_NAMES))
def test_either_web_tool_alone_is_enough_to_stay_silent(
    tool_name: str,
) -> None:
    # ``web_fetch`` counts: 「我點進去看過了」 is a fetch claim, not a
    # search claim, so a deployment with only one of the two still has a
    # character that can honestly reach the web.
    assert browsing_unavailable((tool_name,)) is False
    assert NO_BROWSING_MARK not in _text((tool_name, "generate_image"))


def test_a_non_web_capability_set_still_gets_the_line() -> None:
    assert NO_BROWSING_MARK in _text(("generate_image", "send_photo"))


# --- placement -------------------------------------------------------


def test_the_section_sits_at_the_tail_of_the_cacheable_prefix() -> None:
    """Immediately after the tool rail, ahead of the per-turn zone.

    Pinned because the reason is invisible from the table: everything
    from ``presence_frame`` down is re-derived per turn, and parking a
    stable block behind them would cost the upstream prompt cache the
    whole run.
    """
    order = list(SECTION_ORDER)
    assert order[order.index("tools") + 1] == "honesty_discipline"
    assert order.index("honesty_discipline") < order.index("presence_frame")
