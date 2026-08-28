"""What the merge prompt actually says.

Its own module because it needs no event loop and no adapter — the
template is rendered directly. Keeping it out of ``test_llm_merger``
avoids an ``asyncio`` mark on a set of synchronous string assertions.

These are not style checks. All three of DH3's prompt red lines
(merge-don't-restate, time-neutral wording, a stated length ceiling)
live in the template text and nowhere else, so a template that quietly
lost one would still render, still parse, and still produce a plausible
summary that rots a few days later.
"""

from __future__ import annotations

from kokoro_link.infrastructure.dialogue.llm_checkpoint_merger import (
    MAX_SUMMARY_CHARS,
)
from kokoro_link.infrastructure.prompts import get_default_loader


def _render() -> str:
    return get_default_loader().render(
        "dialogue/checkpoint_merge",
        character_name="小悠",
        previous_summary="（尚無）",
        transcript="使用者：測試",
        max_chars=str(MAX_SUMMARY_CHARS),
    )


def test_the_prompt_asks_for_a_merge_not_a_restatement() -> None:
    text = _render()
    assert "合併" in text
    assert "逐字抄" in text


def test_the_prompt_forbids_relative_time_words_by_name() -> None:
    """Naming them is the point. "avoid relative time" is advice a model
    will nod at and ignore; a list of banned words is a rule."""
    text = _render()
    for word in ("剛剛", "今天早上", "昨天", "待會", "前幾天"):
        assert word in text


def test_the_prompt_states_the_length_ceiling_it_is_enforced_at() -> None:
    """The truncation above is a backstop. If the two numbers disagreed,
    every long summary would be cut mid-sentence instead of composed to
    fit."""
    assert str(MAX_SUMMARY_CHARS) in _render()


def test_the_prompt_asks_for_traditional_chinese_prose() -> None:
    text = _render()
    assert "繁體中文" in text
    assert "不分段" in text
