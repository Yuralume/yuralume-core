"""Shared helper for injecting the operator's primary language as a
prompt-fact across every LLM job that produces user-visible content.

Why a one-liner helper instead of templating it into each builder
separately: *every* LLM job producing operator-visible content must
see the same fact, in the same shape, so the model never gets a
different language signal between
chat, proactive, planner, feed, story etc. Routing this through a
single function keeps the wording uniform and makes it cheap to tune
later (one edit propagates everywhere).

Per the LLM-first rule, this is a **fact** injection — we
state the language and let the model handle it. No code-level
branching on the tag. Callers should NOT pre-filter "is this zh-TW?"
before calling; one extra line of prompt is cheaper than an
inconsistent fact layer.

**Why the tag alone was not enough.** The line used to carry only the
BCP 47 code. A code is an unambiguous key for us and a weak signal for
a model — ``zh-TW`` reliably yields Chinese but not reliably
*Traditional* Chinese, and smaller models drift to Simplified from a
bare tag more often than from a named language. So each tag also
carries its endonym, and, where the language is written in more than
one script, an explicit script requirement. That second part is a
property of the language (Chinese has two scripts for the same words),
not a patch for one observed failure — the table has a slot for it and
languages that do not need one leave it empty.

The prompt line is a first line of defence, not a guarantee: models
still drift occasionally, which is why non-streaming output also
passes the deterministic backstop in
:mod:`kokoro_link.infrastructure.localization.chinese_script`.
"""

from __future__ import annotations

from typing import Final, NamedTuple


class _LanguageFacts(NamedTuple):
    """How one language should be described to a model."""

    name: str
    """The language's own name, which models anchor on far more
    strongly than a BCP 47 code."""
    script_requirement: str = ""
    """Explicit orthography demand, for languages written in more than
    one script. Empty for languages where the name settles it."""


_LANGUAGE_FACTS: Final[dict[str, _LanguageFacts]] = {
    "zh-tw": _LanguageFacts(
        "繁體中文（台灣）",
        "必須全程使用繁體字（正體字），一個簡體字都不能出現；"
        "用詞也用台灣的習慣說法（例如「影片」不是「视频」、"
        "「軟體」不是「软件」、「網路」不是「网络」、「資訊」不是「信息」）。",
    ),
    "zh-hant": _LanguageFacts(
        "繁體中文",
        "必須全程使用繁體字（正體字），一個簡體字都不能出現。",
    ),
    "zh-hk": _LanguageFacts(
        "繁體中文（香港）",
        "必須全程使用繁體字，一個簡體字都不能出現。",
    ),
    "zh-cn": _LanguageFacts(
        "简体中文",
        "全程使用简体字。",
    ),
    "zh-hans": _LanguageFacts(
        "简体中文",
        "全程使用简体字。",
    ),
    "ja": _LanguageFacts("日本語"),
    "ja-jp": _LanguageFacts("日本語"),
    "en": _LanguageFacts("English"),
    "en-us": _LanguageFacts("English"),
    "en-gb": _LanguageFacts("English"),
    "ko": _LanguageFacts("한국어"),
    "ko-kr": _LanguageFacts("한국어"),
}
"""Tag → how to describe it. Unlisted tags fall back to the bare code,
which is the pre-existing behaviour: an unfamiliar language still gets
a correct instruction, just a weaker one."""


def render_operator_language_hint(language_tag: str | None) -> str:
    """Return a single line stating the operator's content language.

    Returns ``""`` when the tag is missing — callers can pipe the
    result straight into a list of prompt lines without conditionals
    on their side.

    The wording is intentionally bilingual-light: Chinese leading
    label (most prompts are Chinese-scaffolded today) plus the BCP 47
    code itself, which is universal, plus the language's own name so
    the model has a strong anchor rather than a code to interpret.
    """
    tag = (language_tag or "").strip()
    if not tag:
        return ""
    facts = _LANGUAGE_FACTS.get(tag.lower().replace("_", "-"))
    named = f"{facts.name}（BCP 47：{tag}）" if facts else f"{tag}（BCP 47 標籤）"
    line = (
        f"玩家可見自然語言輸出語言：{named}。"
        "所有玩家會看到的自然語言內容都必須使用此語言；"
        "引用既有訊息、專有名詞、程式碼或固定格式欄位時保留原文。"
    )
    if facts and facts.script_requirement:
        line += facts.script_requirement
    return line


def render_operator_language_lines(language_tag: str | None) -> list[str]:
    """List-shaped variant for builders that compose prompts from a
    list of lines. Empty list when the tag is missing, so callers can
    splat it (``*lines, ...``) without branches."""
    line = render_operator_language_hint(language_tag)
    return [line] if line else []
