"""A tokenizer-free size estimate for prompt text.

``ChatModelPort.generate`` is ``str -> str`` (``contracts/llm.py``) and
returns no usage block, so nothing downstream ever learns what a prompt
actually cost. Pulling a real tokenizer in would mean either a
per-provider vocabulary (which the provider-independence red line
forbids) or a large dependency whose numbers would still be wrong for
every provider but one.

So this module estimates, and says so loudly:

**The numbers are for relative budgeting only. Treat them as ±30%.**

That is enough for every question this codebase asks of them — "is the
uncovered backlog big enough to be worth a merge call", "did the merge
actually shrink anything", "how many older turns still fit under the
dialogue budget". It is *not* enough to compare against a provider's
hard context limit, to bill anybody, or to decide anything a user sees
a number for. Do not use it for those.

The model
--------
Two classes of character, one weight each:

* **CJK** — Han, kana, Hangul, and the full-width punctuation that comes
  with them: ``1.0`` token per character. Real BPE vocabularies land
  between roughly 0.6 and 1.5 tokens per Han character depending on how
  much Chinese the vocabulary was trained on; 1.0 is the middle of that
  spread and errs high for common words, low for rare ones.
* **everything else** — ``len / 4``, the long-standing rule of thumb for
  English-ish text in a byte-pair vocabulary.

Whitespace counts as "everything else" rather than being stripped: a
prompt's newlines and indentation really are tokens.
"""

from __future__ import annotations

import math
from typing import Final

CJK_TOKENS_PER_CHAR: Final[float] = 1.0
"""Weight for one CJK character. See the module docstring for the range
this is the middle of."""

LATIN_CHARS_PER_TOKEN: Final[float] = 4.0
"""Characters per token for everything that is not CJK."""

_CJK_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x1100, 0x11FF),    # Hangul Jamo
    (0x2E80, 0x2EFF),    # CJK Radicals Supplement
    (0x3000, 0x303F),    # CJK Symbols and Punctuation (、。「」…)
    (0x3040, 0x309F),    # Hiragana
    (0x30A0, 0x30FF),    # Katakana
    (0x3100, 0x312F),    # Bopomofo
    (0x3130, 0x318F),    # Hangul Compatibility Jamo
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xA960, 0xA97F),    # Hangul Jamo Extended-A
    (0xAC00, 0xD7AF),    # Hangul Syllables
    (0xD7B0, 0xD7FF),    # Hangul Jamo Extended-B
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0xFE30, 0xFE4F),    # CJK Compatibility Forms
    (0xFF00, 0xFF60),    # Fullwidth forms (！＂＃ … ｀)
    (0xFFE0, 0xFFE6),    # Fullwidth currency signs
    (0x20000, 0x2A6DF),  # CJK Unified Ideographs Extension B
    (0x2A700, 0x2EBEF),  # Extensions C-F
    (0x2F800, 0x2FA1F),  # CJK Compatibility Ideographs Supplement
)
"""Code-point ranges billed at :data:`CJK_TOKENS_PER_CHAR`.

Full-width punctuation is deliberately included: 「」、。！？ are as
expensive as the characters around them and a Traditional-Chinese reply
is full of them. Half-width ASCII punctuation is not — it packs into
neighbouring tokens and is already covered by the ``len / 4`` side.
"""


def is_cjk_char(char: str) -> bool:
    """True when ``char`` is billed at the CJK weight.

    Single-character input by contract; a longer string tests only its
    first character, which is what every caller here wants.
    """
    if not char:
        return False
    code = ord(char[0])
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
        if code < low:
            # Ranges are ascending, so nothing further can match.
            return False
    return False


def count_cjk_chars(text: str) -> int:
    """How many characters of ``text`` are billed at the CJK weight."""
    return sum(1 for char in text if is_cjk_char(char))


def estimate_tokens(text: str) -> int:
    """Estimate the token cost of ``text``. **±30% — relative use only.**

    Total and never raises: empty or ``None``-ish input is ``0``. The
    result is rounded **up**, so any non-empty string costs at least one
    token and a budget check can never be fooled by a fractional
    remainder.
    """
    if not text:
        return 0
    cjk = count_cjk_chars(text)
    other = len(text) - cjk
    return math.ceil(
        cjk * CJK_TOKENS_PER_CHAR + other / LATIN_CHARS_PER_TOKEN,
    )


def estimate_total_tokens(texts: object) -> int:
    """Sum :func:`estimate_tokens` over an iterable of strings.

    Each element is estimated (and therefore rounded up) on its own, the
    way the prompt will actually carry them — as separate lines, not one
    concatenated blob.
    """
    if not texts:
        return 0
    if isinstance(texts, str):
        return estimate_tokens(texts)
    return sum(estimate_tokens(str(item or "")) for item in texts)


__all__ = [
    "CJK_TOKENS_PER_CHAR",
    "LATIN_CHARS_PER_TOKEN",
    "count_cjk_chars",
    "estimate_tokens",
    "estimate_total_tokens",
    "is_cjk_char",
]
