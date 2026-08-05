"""Hosted-mode tone policy for story arcs and arc templates (GF6).

``tone`` is a free-form string that rides all the way into the prompts
that write scenes (``story/beat_scene_writer``, ``story/scene_opener``,
``story/expander_scene``). Self-host that is a feature — an author can
invent a shade without a code change, and the ``mature`` register is
explicitly allowed to write violence / intimacy / cruelty without
flinching.

Hosted (cloud mode) it is a compliance hole: Yuralume's own terms and
AUP promise the hosted service does **not** produce sexually explicit
content, so a tone that instructs the model to write it can't be
reachable from a paid hosted seat.
(``docs/plans/GROWTH_FUNNEL_PLAN.md`` §3.6 hole 1, red line #5.)

Two deliberately different strictnesses live here, because the two
boundaries have opposite failure costs:

``fold_stored_tone`` — the **write** boundary (wizard save, template
    PATCH, character-card import). It touches operator-authored data,
    so it rewrites only what is actually forbidden and leaves every
    other value (including unknown shades) untouched. "Don't blow up
    the data" wins over "normalise everything".

``resolve_prompt_tone`` — the **render** boundary (what reaches the
    model). This is an allow-list: hosted, anything outside the known
    catalogue collapses to the neutral default, because the raw string
    is interpolated verbatim into the prompt and an off-catalogue tone
    is an unbounded instruction channel ("露骨，越詳細越好" is a valid
    free-form tone today). Fail closed.

Both are no-ops when ``cloud_mode`` is false, so self-host behaviour is
byte-identical to before this module existed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable


_LOGGER = logging.getLogger(__name__)


TONE_DAILY = "daily"
TONE_DRAMATIC = "dramatic"
TONE_MATURE = "mature"
TONE_DARK = "dark"
TONE_LIGHTHEARTED = "lighthearted"

SELECTABLE_TONES: tuple[str, ...] = (
    TONE_DAILY,
    TONE_DRAMATIC,
    TONE_MATURE,
    TONE_DARK,
    TONE_LIGHTHEARTED,
)
"""The catalogue the wizard offers and the intake prompts name.

Order is the UI order; the wizard's ``/arc-templates/scaffolds``
catalogue is derived from this tuple and the frontend holds the
trilingual labels."""

CLOUD_BLOCKED_TONES = frozenset({TONE_MATURE})
"""Tones a hosted deployment must never author, store, or render."""

CLOUD_BLOCKED_SUBSTITUTE = TONE_DRAMATIC
"""What a blocked tone becomes hosted.

``dramatic`` is the nearest neighbour that keeps the weight the author
asked for (heightened stakes, willing to be heavy) without the
"暴力、肉體、權力支配、酒精、性、創傷的細節都可以據實寫" directive
that makes ``mature`` non-compliant."""

CLOUD_UNKNOWN_SUBSTITUTE = TONE_DAILY
"""What an off-catalogue tone renders as hosted.

Matches the expander's pre-existing unknown-tone behaviour (unknown
labels already fell through to the ``daily`` framing), so the only
thing this changes is that the raw string stops reaching the model."""


def _clean(tone: str | None) -> str:
    return (tone or "").strip()


def is_blocked_in_cloud(tone: str | None) -> bool:
    """True when ``tone`` is one a hosted deployment must refuse."""
    return _clean(tone).casefold() in CLOUD_BLOCKED_TONES


def selectable_tones(*, cloud_mode: bool) -> tuple[str, ...]:
    """Catalogue to offer the operator (and to name in LLM prompts)."""
    if not cloud_mode:
        return SELECTABLE_TONES
    return tuple(
        tone for tone in SELECTABLE_TONES if tone not in CLOUD_BLOCKED_TONES
    )


def tone_vocabulary(*, cloud_mode: bool, separator: str = " / ") -> str:
    """The catalogue rendered for a prompt line ("daily / dramatic / …")."""
    return separator.join(selectable_tones(cloud_mode=cloud_mode))


def filter_suggested_tones(
    tones: Iterable[str], *, cloud_mode: bool,
) -> list[str]:
    """Drop blocked tones from an LLM's suggestion list.

    The prompt already omits them from the vocabulary; a model that
    proposes one anyway must not get it in front of the operator."""
    if not cloud_mode:
        return list(tones)
    return [tone for tone in tones if not is_blocked_in_cloud(tone)]


def fold_stored_tone(
    tone: str | None, *, cloud_mode: bool, context: str = "",
) -> str:
    """Write boundary — replace a forbidden tone before it is persisted.

    Returns ``tone`` unchanged outside cloud mode, and hosted for every
    value that isn't explicitly blocked. The substitution is visible to
    the caller (every write surface echoes the saved row back), so this
    is a correction the operator can see, not a silent rewrite."""
    original = tone or ""
    if not cloud_mode or not is_blocked_in_cloud(original):
        return original
    _LOGGER.info(
        "hosted tone policy: folding stored tone %r -> %r (%s)",
        _clean(original),
        CLOUD_BLOCKED_SUBSTITUTE,
        context or "unspecified surface",
    )
    return CLOUD_BLOCKED_SUBSTITUTE


def resolve_prompt_tone(
    tone: str | None, *, cloud_mode: bool, context: str = "",
) -> str:
    """Render boundary — the tone label/profile that may reach the model.

    Blank stays blank so callers keep their own "（未指定）" handling.
    Outside cloud mode this is the identity function."""
    original = tone or ""
    cleaned = _clean(original)
    if not cloud_mode or not cleaned:
        return original
    folded = cleaned.casefold()
    if folded in SELECTABLE_TONES and folded not in CLOUD_BLOCKED_TONES:
        return cleaned
    substitute = (
        CLOUD_BLOCKED_SUBSTITUTE
        if folded in CLOUD_BLOCKED_TONES
        else CLOUD_UNKNOWN_SUBSTITUTE
    )
    _LOGGER.info(
        "hosted tone policy: rendering tone %r as %r (%s)",
        cleaned, substitute, context or "unspecified prompt",
    )
    return substitute
