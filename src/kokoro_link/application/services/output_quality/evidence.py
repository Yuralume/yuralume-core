"""Deterministic *evidence* about a candidate — never a verdict (D6).

The LLM-first red line for this batch is narrow and load-bearing: code may
compute structural facts and hand them to the judge, and may not decide
anything from them. A regex that recognises a leaked schema tag and drops
the reply is exactly the kind of brittle content branch these gates exist
to replace; a line that says "正文長度 612 超過上限 280" is a fact the judge
can weigh against everything else it sees.

So every function here returns *strings for a prompt*. None of them return
a boolean the caller could branch on to intercept, none of them rewrite
text, and none of them carry a word list. They are pure and cheap enough to
call on every composition.

Callers put the results into
:attr:`~kokoro_link.contracts.novelty_gate.NoveltyGateContext.mechanical_evidence_lines`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

_FOREIGN_SHARE_REPORT_THRESHOLD = 0.4
"""Per-line *foreign-script* share at which a line is worth naming.

"Foreign" is relative to the operator's own writing system family (see
:func:`_primary_script_family`), because "mixed" has no meaning without a
reference: measured against Latin alone, every reply an en/vi/id/es
operator ever receives is 100% "mixed", and the chat risk score that reads
this evidence would buffer their every turn forever.

A reporting threshold, not a rule: crossing it changes what the evidence
*mentions*, never what happens to the text. The judge still decides whether
a mixed-script reply is 晶晶體 or a perfectly ordinary quoted product name.
"""

_FAMILY_CJK = "cjk"
_FAMILY_LATIN = "latin"
_FAMILY_OTHER = "other"

_CJK_PRIMARY_SUBTAGS: frozenset[str] = frozenset({
    "zh", "cmn", "yue", "wuu", "nan", "hak", "ja", "ko",
    # ISO 639-2/T (and the 639-2/B alternative for Chinese) three-letter
    # forms of the same four labels — normalise_language_tag() accepts any
    # 2-3 letter subtag, so a profile stored as "jpn"/"kor"/"zho"/"chi"
    # reaches this table too, not just the 639-1 two-letter forms above.
    "jpn", "kor", "zho", "chi",
})
"""Primary language subtags written in a CJK script.

Routing configuration, not a content rule: this table maps a *language
label the operator chose in their profile* onto a writing system, and it
never looks at the text.
"""

_OTHER_SCRIPT_PRIMARY_SUBTAGS: frozenset[str] = frozenset({
    # Cyrillic
    "ru", "uk", "be", "bg", "sr", "mk", "kk", "ky", "tg", "mn",
    # Arabic
    "ar", "fa", "ps", "sd", "ug", "ur",
    # Hebrew
    "he", "iw",
    # Greek
    "el",
    # Indic / Brahmic
    "hi", "mr", "ne", "bn", "pa", "gu", "or", "ta", "te", "kn", "ml", "si",
    # Southeast/Central Asian
    "th", "lo", "km", "my", "bo",
    # Other distinct scripts
    "ka", "hy", "am", "ti",
})
"""Primary language subtags written in a script that is neither CJK nor
Latin — Thai, Russian, Arabic and the like.

Same kind of routing table as :data:`_CJK_PRIMARY_SUBTAGS`: it maps a
language *label*, never the text itself, onto a writing-system family so
:func:`_primary_script_family` has a third answer besides CJK and Latin.
Without it, every operator writing in one of these scripts fell through to
the Latin default (see that function) and their "drifted into English"
signal was measured against the wrong reference — Latin was treated as
*home*, so a reply that actually drifted into English read as 0% foreign.
Anything not listed here and not in :data:`_CJK_PRIMARY_SUBTAGS` still
falls back to Latin, which stays a safe default for the many Latin-script
languages this table does not enumerate.
"""

_MAX_REPORTED_LINES = 5
"""How many individual lines the mix summary names before it stops.

Evidence competes for prompt budget with the material the reply is
actually about; a full per-line dump of a long history would win that
competition for no gain."""


def length_overrun_lines(raw_text: str, cap: int) -> tuple[str, ...]:
    """One evidence line when *raw_text* runs past *cap* characters.

    Replaces the old silent hard truncation (D6): the model is told its
    draft ran long and given a chance to write a shorter one, instead of
    the player receiving a sentence that stops mid-word. Callers that
    still need a hard cap apply it *after* the regeneration this evidence
    buys, not instead of it.

    Empty tuple whenever there is nothing to say — no text, no cap, or a
    draft inside the limit — so a caller can splice the result into its
    evidence tuple unconditionally.
    """
    text = raw_text or ""
    if cap <= 0 or not text:
        return ()
    length = len(text)
    if length <= cap:
        return ()
    return (
        f"正文長度 {length} 字元，超過上限 {cap} 字元"
        f"（超出 {length - cap} 字元）；疑混入非正文內容（如 prompt、"
        "標記、schema 片段或未收尾的續寫）。",
    )


def script_mix_lines(
    texts: Sequence[str],
    *,
    primary_language: str = "",
) -> tuple[str, ...]:
    """Descriptive script composition of recent outputs (D3).

    Counts letters by script family — CJK (ideographs, kana, hangul),
    Latin, everything else — over the supplied lines, and describes what
    it found. Digits, punctuation, whitespace and emoji are excluded from
    the denominator: they carry no language signal and would otherwise let
    a heavily punctuated line read as a script shift.

    *primary_language* is the operator's own language label (``"zh-TW"``,
    ``"en"``, ``"th"``…). It decides the operator's *home* script family —
    CJK, Latin, or other (Thai, Russian, Arabic, and the rest of the
    scripts that are neither CJK nor Latin; see
    :data:`_OTHER_SCRIPT_PRIMARY_SUBTAGS`, which does not subdivide further)
    — and the per-line flag then names whatever share of a line falls
    *outside* that home family: Latin for a CJK operator, CJK for a Latin
    operator, and Latin-or-CJK together for an other-script operator (their
    home script, like everyone else's "other" text, lands in the same
    unclassified bucket, so it never counts as foreign against itself).
    Without a home family the flag degenerates into "contains Latin", so a
    perfectly ordinary English reply to an English-speaking operator reads
    as a language drift on every single turn — and a Thai/Russian/Arabic
    operator's reply drifting into English read as *zero* drift, because
    Latin was silently treated as home for anyone not on the CJK list. The
    chat risk score that reads this evidence would either buffer the first
    operator's every turn forever, or never buffer the second's at all. The
    opening composition summary is unaffected: it reports all three
    families and is the same sentence for everyone.

    The default ``""`` keeps the historical CJK reference, so callers with
    no operator language to hand (and every unmigrated one) behave exactly
    as before.

    Two uses, one computation: it goes into the gate context as evidence
    for the ``language_mismatch`` axis, and into chat's pre-generation risk
    score so a character that has started mixing scripts is routed into the
    buffered (gated) path for the following turns. Neither use decides
    anything about *this* text on its own.

    Empty tuple when there is nothing countable — no lines, or lines with
    no letters at all.
    """
    lines = [text for text in (texts or ()) if text and text.strip()]
    if not lines:
        return ()
    family = _primary_script_family(primary_language)
    if family == _FAMILY_CJK:
        foreign_label = "拉丁字母"
    elif family == _FAMILY_LATIN:
        foreign_label = "中日韓文字"
    else:
        foreign_label = "拉丁字母或中日韓文字"
    per_line: list[tuple[int, float, int]] = []  # (index, foreign share, letters)
    cjk_total = latin_total = other_total = 0
    for index, text in enumerate(lines, start=1):
        cjk, latin, other = _script_counts(text)
        letters = cjk + latin + other
        cjk_total += cjk
        latin_total += latin
        other_total += other
        if letters:
            if family == _FAMILY_CJK:
                foreign = latin
            elif family == _FAMILY_LATIN:
                foreign = cjk
            else:
                foreign = cjk + latin
            per_line.append((index, foreign / letters, letters))
    grand_total = cjk_total + latin_total + other_total
    if not grand_total:
        return ()
    out = [
        f"近 {len(lines)} 則輸出共 {grand_total} 個文字字元，"
        f"組成：中日韓 {_pct(cjk_total, grand_total)}、"
        f"拉丁字母 {_pct(latin_total, grand_total)}、"
        f"其他 {_pct(other_total, grand_total)}。",
    ]
    flagged = [
        (index, share)
        for index, share, _letters in per_line
        if share > _FOREIGN_SHARE_REPORT_THRESHOLD
    ]
    if flagged:
        shown = flagged[:_MAX_REPORTED_LINES]
        detail = "、".join(
            f"第 {index} 則 {round(share * 100)}%" for index, share in shown
        )
        tail = "" if len(flagged) <= len(shown) else f"（另有 {len(flagged) - len(shown)} 則）"
        out.append(
            f"其中 {len(flagged)} 則的{foreign_label}占比超過 "
            f"{round(_FOREIGN_SHARE_REPORT_THRESHOLD * 100)}%：{detail}{tail}。",
        )
    return tuple(out)


def _primary_script_family(primary_language: str) -> str:
    """Which script family the operator writes in — CJK, Latin, or other.

    Reads only the primary subtag, so ``zh``, ``zh-TW``, ``zh_Hant_TW`` and
    ``ZH-CN`` all answer the same thing. Blank falls back to CJK, which is
    the behaviour that predates this parameter; an unrecognised subtag
    falls back to Latin, which stays a safe default for the many
    Latin-script languages :data:`_OTHER_SCRIPT_PRIMARY_SUBTAGS` does not
    enumerate.
    """
    tag = (primary_language or "").strip().lower()
    if not tag:
        return _FAMILY_CJK
    subtag = tag.replace("_", "-").split("-", 1)[0]
    if subtag in _CJK_PRIMARY_SUBTAGS:
        return _FAMILY_CJK
    if subtag in _OTHER_SCRIPT_PRIMARY_SUBTAGS:
        return _FAMILY_OTHER
    return _FAMILY_LATIN


def _pct(part: int, total: int) -> str:
    return f"{round((part / total) * 100)}%" if total else "0%"


def _script_counts(text: str) -> tuple[int, int, int]:
    """``(cjk, latin, other)`` letter counts for one string."""
    cjk = latin = other = 0
    for char in text:
        if not char.isalpha():
            continue
        if _is_cjk(char):
            cjk += 1
        elif _is_latin(char):
            latin += 1
        else:
            other += 1
    return cjk, latin, other


def _is_latin(char: str) -> bool:
    try:
        return "LATIN" in unicodedata.name(char)
    except ValueError:  # unnamed codepoint — not something we can classify
        return False


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x30FF),   # hiragana + katakana
    (0x3400, 0x4DBF),   # CJK ext A
    (0x4E00, 0x9FFF),   # CJK unified ideographs
    (0xAC00, 0xD7AF),   # hangul syllables
    (0xF900, 0xFAFF),   # CJK compatibility ideographs
    (0x20000, 0x2FA1F),  # CJK ext B..F + compatibility supplement
)


__all__ = ["length_overrun_lines", "script_mix_lines"]
