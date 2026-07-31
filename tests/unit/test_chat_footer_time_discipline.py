"""Chat instructions footer carries the time-anchor discipline (CF6b / P6c).

Before this, the composition-side rule lived only in the tuned overlay and
only covered the 「長期記憶」 block. But the 7/28 芊璃 dump proved three other
sections ship frozen relative words into the prompt — 「角色目標」 (a goal
written as 「明早一起出門吃刨冰」), 「故事線」 beats, and the invite block.
The footer therefore has to name the single date authority (the
「接下來幾天的行程」 block, which renders real ISO dates) and forbid reciting
a stale 「明天」 that doesn't match it.

The tuned overlay (parent repo ``prompt-packs/tuned/chat/instructions_footer``)
carries the same discipline in its own wording; this test pins the baseline
that ships with Core.
"""

from __future__ import annotations

from kokoro_link.infrastructure.prompts import get_default_loader

_RESPONSE_FORMAT = "回覆格式慣例：口語台詞直接寫，不要加引號。"


def _footer() -> str:
    return get_default_loader().render(
        "chat/instructions_footer",
        response_format_instruction=_RESPONSE_FORMAT,
    )


def test_footer_keeps_long_term_memory_anchor_rule() -> None:
    footer = _footer()

    assert "約 N 天前" in footer
    assert "錨點" in footer


def test_footer_extends_time_discipline_beyond_memories() -> None:
    footer = _footer()

    for section in ("角色目標", "故事線", "邀請"):
        assert section in footer


def test_footer_names_the_schedule_block_as_the_only_date_authority() -> None:
    footer = _footer()

    assert "接下來幾天的行程" in footer
    assert "唯一" in footer


def test_footer_forbids_reciting_frozen_relative_words() -> None:
    footer = _footer()

    assert "明天" in footer
    assert "照唸" in footer or "照字面" in footer


def test_footer_preserves_existing_rails() -> None:
    """CF6b must not regress the highest-priority sensitive-memory rule, the
    visible-language pin, or the response-format placeholder."""
    footer = _footer()

    assert "最高原則" in footer
    assert "玩家可見自然語言輸出語言" in footer
    assert _RESPONSE_FORMAT in footer
