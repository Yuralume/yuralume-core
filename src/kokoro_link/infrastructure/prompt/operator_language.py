"""Shared helper for injecting the operator's primary language as a
prompt-fact across every LLM job that produces user-visible content.

Why a one-liner helper instead of templating it into each builder
separately: FRONTEND_I18N_PLAN.md mandates that *every* LLM job
producing operator-visible content sees the same fact, in the same
shape, so the model never gets a different language signal between
chat, proactive, planner, feed, story etc. Routing this through a
single function keeps the wording uniform and makes it cheap to tune
later (one edit propagates everywhere).

Per CLAUDE.md's LLM-first rule, this is a **fact** injection — we
state the BCP 47 tag and let the model handle language. No
code-level branching on the tag. Callers should NOT pre-filter "is
this zh-TW?" before calling; one extra line of prompt is cheaper
than an inconsistent fact layer.
"""

from __future__ import annotations


def render_operator_language_hint(language_tag: str | None) -> str:
    """Return a single line stating the operator's content language.

    Returns ``""`` when the tag is missing — callers can pipe the
    result straight into a list of prompt lines without conditionals
    on their side.

    The wording is intentionally bilingual-light: Chinese leading
    label (most prompts are Chinese-scaffolded today) plus the BCP 47
    code itself, which is universal. The model treats this as a fact
    and adapts its output language accordingly.
    """
    tag = (language_tag or "").strip()
    if not tag:
        return ""
    return (
        f"玩家可見自然語言輸出語言（BCP 47 標籤）：{tag}。"
        "所有玩家會看到的自然語言內容都必須使用此語言；"
        "引用既有訊息、專有名詞、程式碼或固定格式欄位時保留原文。"
    )


def render_operator_language_lines(language_tag: str | None) -> list[str]:
    """List-shaped variant for builders that compose prompts from a
    list of lines. Empty list when the tag is missing, so callers can
    splat it (``*lines, ...``) without branches."""
    line = render_operator_language_hint(language_tag)
    return [line] if line else []
