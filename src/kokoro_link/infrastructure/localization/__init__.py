"""Deterministic localization helpers for backend-composed player text.

This package is **deliberately NOT an LLM path**. It holds the static
fallback / channel-wrapper strings the backend emits without a model in
the loop (LLM-failure apologies, channel attachment wrappers, inbound
placeholders). Per CLAUDE.md the LLM-first rule governs *semantic*
output; these are fixed system strings with no semantics to reason
about, so a per-locale dict is the correct — and only — way to keep an
en-US / ja-JP operator from receiving zh-TW system text.

The single precedent this mirrors is
``notification_service._fallback_body`` (per-language dict keyed by the
recipient language) and ``llm_arc_planner._synthetic_template_pack``
(exact-tag → language-subtag family → zh-TW fallback resolution).
"""

from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
    resolve_fallback_language,
)

__all__ = [
    "localized_fallback_text",
    "resolve_fallback_language",
]
