"""Simplified → Traditional Chinese script normalisation.

Even with the operator's content language stated as a prompt fact, an
LLM occasionally emits Simplified Chinese for a Traditional-Chinese
player — more often on smaller / quantised models, and unpredictably
enough that no amount of prompt tuning removes the tail. This module is
the deterministic backstop: a whole-text pass that maps the glyphs back,
applied to LLM output before it reaches the player.

Three properties make this safe as a blanket post-process rather than a
per-case patch (the "no keyword special-casing" rule):

* **Idempotent on already-Traditional text.** Text that is already in
  the target script comes out byte-identical, so running it everywhere
  costs nothing but a dictionary lookup.
* **Blind to content.** ASCII, JSON structure, URLs and code are
  untouched; only Han characters that have a Traditional counterpart
  move. It normalises a *script*, it does not interpret meaning.
* **Word-aware, not character-mapped.** One Simplified character can
  map to several Traditional ones (发 → 發 / 髮, 干 → 乾 / 幹 / 干,
  后 → 後 / 后). OpenCC disambiguates with a phrase dictionary, which
  is why this operates on whole texts and never on stream fragments —
  see :mod:`kokoro_link.infrastructure.llm.script_normalizer`.

**Glyphs only, deliberately.** The ``s2tw`` profile converts the script
and Taiwan variant forms; it does NOT swap regional vocabulary
(``网络`` becomes ``網絡``, not ``網路``; ``软件`` becomes ``軟件``, not
``軟體``). OpenCC's ``s2twp`` profile would do that, and is deliberately
NOT used: phrase substitution rewrites words the character actually
chose, and mistranslates across regional senses (``土豆`` is potato in
one register and peanut in another). Wording stays a prompt concern;
this layer only guarantees the script.

**Untouched text short-circuits.** Conversion runs first and its result
is discarded when nothing moved, so a reply that was already in the
target script leaves byte-identical. That guard is what makes the
``臺`` → ``台`` fold below safe: it only ever applies to text OpenCC
just rewrote, i.e. text the model had emitted in Simplified.

**Optional dependency.** ``opencc`` is declared in ``pyproject.toml``
but self-hosted installs may run without it. Its absence degrades to a
pass-through with a single warning rather than breaking chat.
"""

from __future__ import annotations

import logging
from typing import Any, Final

_LOGGER = logging.getLogger(__name__)

_OPENCC_PROFILE: Final = "s2tw"
"""Simplified → Traditional with Taiwan variant forms, no phrase
substitution. See the module docstring for why not ``s2twp``."""

_TRADITIONAL_LANGUAGE_TAGS: Final = frozenset(
    {"zh-tw", "zh-hant", "zh-hant-tw"},
)
"""Language tags whose players expect Traditional Chinese glyphs.

Deliberately narrow. ``zh-HK`` / ``zh-MO`` are Traditional too, but
``s2tw`` normalises toward *Taiwan* variant forms, which is the wrong
target for Hong Kong; they are left alone until there is a reason to
wire ``s2hk``. ``zh-CN`` / ``zh-Hans`` must never normalise — the
player asked for Simplified. Any non-Chinese tag is excluded because
``s2tw`` would corrupt it: Japanese shinjitai shares code points with
Simplified forms (学 → 學, 国 → 國, 体 → 體), so an unguarded pass over
a Japanese reply rewrites it into Chinese glyphs.
"""

_COLLOQUIAL_VARIANTS: Final = (("臺", "台"),)
"""Post-conversion folds toward everyday Taiwanese writing.

OpenCC's phrase dictionary standardises ``台`` to ``臺`` (``台北`` →
``臺北``), which is the official form but reads like government
paperwork in a character's dialogue — everyday writing here is
overwhelmingly ``台``. Applied only when conversion actually changed
something, so a reply the player already received in Traditional is
never rewritten by this rule; it merely finishes the job on text that
arrived Simplified.
"""

_converter: Any | None = None
_converter_loaded = False


def targets_traditional_chinese(language_tag: str | None) -> bool:
    """Whether output for ``language_tag`` should be normalised.

    Case- and separator-insensitive (``zh_TW``, ``zh-tw`` and ``zh-TW``
    all match) because the tag reaches us from several stores. Missing
    or unrecognised tags return ``False`` — normalising the wrong
    language is far worse than leaving a stray Simplified reply.
    """
    tag = (language_tag or "").strip().lower().replace("_", "-")
    return tag in _TRADITIONAL_LANGUAGE_TAGS


def converter_available() -> bool:
    """Whether the OpenCC backend loaded. Loads it on first call."""
    return _load_converter() is not None


def normalize_to_traditional(text: str) -> str:
    """Return ``text`` with Simplified Han characters mapped to
    Traditional (Taiwan variants).

    Callers are responsible for the language gate — this function does
    not inspect the tag and will happily corrupt Japanese if handed it.
    Use :func:`targets_traditional_chinese` first.

    Returns the input unchanged when it holds nothing to convert, when
    the backend is unavailable, or when conversion raises: a
    script-normalisation failure must never cost the player their reply.
    """
    converted, _ = normalize_to_traditional_reporting(text)
    return converted


def normalize_to_traditional_reporting(text: str) -> tuple[str, bool]:
    """:func:`normalize_to_traditional` plus whether anything changed.

    The flag is the only honest signal that a model drifted out of the
    player's script on a given call — nothing upstream reports it — so
    callers that care about how often it happens (and on which model)
    read it from here rather than diffing the strings again.
    """
    if not text:
        return text, False
    converter = _load_converter()
    if converter is None:
        return text, False
    try:
        converted = converter.convert(text)
    except Exception:  # noqa: BLE001 — degrading beats losing the reply
        _LOGGER.exception(
            "chinese script normalisation failed; returning original text",
        )
        return text, False
    if converted == text:
        return text, False
    for official, colloquial in _COLLOQUIAL_VARIANTS:
        converted = converted.replace(official, colloquial)
    return converted, True


def _load_converter() -> Any | None:
    """Lazily build the process-wide OpenCC converter.

    Construction parses the phrase dictionaries, so it is done once and
    cached — including the failure case, so a missing dependency logs a
    single warning instead of one per generated reply.
    """
    global _converter, _converter_loaded
    if _converter_loaded:
        return _converter
    _converter_loaded = True
    try:
        import opencc  # noqa: PLC0415 — optional dependency, load on demand

        _converter = opencc.OpenCC(_OPENCC_PROFILE)
    except Exception:  # noqa: BLE001 — optional dependency
        _LOGGER.warning(
            "opencc unavailable — Simplified→Traditional normalisation is "
            "disabled; player-visible output will be passed through as the "
            "model wrote it. Install the 'opencc-python-reimplemented' "
            "package to enable it.",
        )
        _converter = None
    return _converter
