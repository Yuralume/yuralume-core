"""Ops-time translation of an official character card's catalog content.

The Cloud control plane owns the official-card catalog: the ``.lumecard``
artifact, the per-locale translation rows, the approval state. What it
does **not** own is how card prose is translated — the field policy, the
merge validation and the "never invent, never blank" discipline live in
:mod:`kokoro_link.infrastructure.character_card.llm_translator`, because
that is what the player-facing import path has always used. Reimplementing
them in Java would be a second source of truth that drifts on the first
new profile field (plan §3.4 / D4).

So this module is the seam: a **stateless** text transformer the console
calls, holding nothing and reading nothing.

Two disciplines carried over verbatim from the import translator:

* **Same-length or nothing** for list fields — a shorter list pairs item 2
  with item 3's translation, and no caller can tell which item vanished.
* **Never blank the source** — a field the model dropped, emptied or
  answered with a non-string keeps its original text.

One discipline is new, and it is why this is a separate service rather
than a call into ``translate_profile``: **failure is per field, not per
card**. The console shows the owner exactly which fields came back
untranslated so they can retry those; a whole-card 502 would turn one
stubborn ``note`` field into "the card cannot be translated".

The prompt is inline here rather than in the baseline prompt pack for the
same reason the showcase reviewer's is (SH ruling): an ops tool must not
acquire a dependency on the pack release order — a Core deploy that lands
before the pack would otherwise break the console's button.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from kokoro_link.application.services.feature_keys import (
    FEATURE_OFFICIAL_CARD_TRANSLATE,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.showcase.json_output import (
    coerce_json_object,
)
from kokoro_link.infrastructure.character_card.llm_translator import (
    PROFILE_LIST_FIELDS,
    PROFILE_SCALAR_FIELDS,
    valid_translated_text,
    valid_translated_text_list,
)

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# field policy
# --------------------------------------------------------------------------- #

CARD_META_SCALAR_FIELDS = ("title", "description", "note")
"""Catalog copy shown in the public listing. ``author`` is deliberately
absent — it is a person's name or handle, not prose to localize — and so
are ``app_version`` / ``created_at``, which are not language at all."""

CARD_META_LIST_FIELDS = ("tags",)

TRANSLATABLE_SCALAR_FIELDS = PROFILE_SCALAR_FIELDS + CARD_META_SCALAR_FIELDS
TRANSLATABLE_LIST_FIELDS = PROFILE_LIST_FIELDS + CARD_META_LIST_FIELDS
TRANSLATABLE_FIELDS = frozenset(
    TRANSLATABLE_SCALAR_FIELDS + TRANSLATABLE_LIST_FIELDS,
)
"""Everything this endpoint will rewrite. The payload is a flat map because
profile prose and card meta share one namespace with no collisions
(``name`` is the character's, ``title`` is the card's). Any other key the
caller sends is passed through untouched — the caller decides what its
payload carries, this service decides what is language."""

# --------------------------------------------------------------------------- #
# failure vocabulary
# --------------------------------------------------------------------------- #

REASON_MISSING = "missing"
"""The model's answer had no such key."""

REASON_INVALID = "invalid"
"""Present but unusable: not a string / list of strings, or empty."""

REASON_LENGTH_MISMATCH = "length_mismatch"
"""A list field came back with a different item count."""

REASON_LLM_UNAVAILABLE = "llm_unavailable"
"""No real model is routed for ``official_card_translate`` on this
deployment (control-plane routing not configured). Every field falls back
— the console shows this as "translation not configured", not as a bad
answer."""

REASON_LLM_FAILED = "llm_failed"
"""The call raised, or the answer was not a JSON object."""


@dataclass(frozen=True)
class TranslationNote:
    """One field that came back in the source language, and why."""

    field: str
    reason: str


@dataclass(frozen=True)
class OfficialCardTranslation:
    """The translated payload plus every field that did not make it.

    ``payload`` always has exactly the caller's keys: translated where it
    worked, source text where it did not. An empty ``notes`` means the
    whole payload is translated."""

    payload: dict[str, object]
    notes: tuple[TranslationNote, ...] = ()


class OfficialCardTranslationUnavailable(RuntimeError):
    """No LLM provider is wired at all on this deployment.

    Distinct from :data:`REASON_LLM_UNAVAILABLE`: that one is "nothing is
    routed for this feature key", which degrades to untranslated fields.
    This one means the deployment has no model plumbing whatsoever, which
    is a configuration error the caller should see as 503 rather than as
    a card whose every field mysteriously failed."""


TRANSLATE_SYSTEM_PROMPT = """\
You translate an official character card's catalog content into the target
language. The card is published in a public catalog where players browse
in their own language, so the result is read by strangers, not by the
card's author.

The input is a flat JSON object: each key is one field, each value is a
string or an array of strings.

Rules:
- Translate every field present in the input.
- Output a JSON object with exactly the same keys. Do not add or drop keys.
- For array fields keep the same item count and order.
- `title`, `description`, `tags` and `note` are catalog copy: keep them the
  same length class as the source, do not turn them into marketing prose,
  do not add information the source does not state.
- Names should be natural in the target language: transliterate or localize
  when appropriate, and leave them as-is when already suitable.
- Pronouns should be natural pronouns in the target language, not literal
  word-by-word output.
- If a value is already in the target language, keep it unchanged.
- Do not add explanations, markdown, comments, or fields that were not in
  the input.
- Output valid JSON only.
"""


class OfficialCardTranslator:
    """Translates one card payload into one language per call.

    One call per card rather than per field: the fields of a card are one
    voice and translating them together keeps that voice consistent, and
    the per-field merge validation below already contains the failure a
    batch would otherwise smear across neighbours.
    """

    def __init__(self, resolver: ModelResolver) -> None:
        self._resolver = resolver

    async def translate(
        self,
        payload: Mapping[str, object],
        *,
        target_language: str,
    ) -> OfficialCardTranslation:
        target = (target_language or "").strip()
        if not target:
            raise ValueError("target_language is required")

        source = dict(payload or {})
        translatable = {
            name: value
            for name, value in source.items()
            if name in TRANSLATABLE_FIELDS and _has_content(value)
        }
        if not translatable:
            # Nothing to say to a model: an all-empty or all-pass-through
            # payload is a success with zero notes, not a failure.
            return OfficialCardTranslation(payload=source)

        try:
            if await self._resolver.is_fake():
                return _all_untranslated(source, translatable, REASON_LLM_UNAVAILABLE)
            raw = await self._resolver.generate(
                _build_prompt(translatable, target_language=target),
            )
        except Exception:  # noqa: BLE001 — the console decides what to retry
            _LOGGER.exception(
                "official card translation to %s failed", target,
            )
            return _all_untranslated(source, translatable, REASON_LLM_FAILED)

        parsed = coerce_json_object(raw)
        if parsed is None:
            _LOGGER.warning(
                "official card translation to %s returned no JSON object", target,
            )
            return _all_untranslated(source, translatable, REASON_LLM_FAILED)
        return _merge(source, translatable, parsed)


def build_official_card_translator(container) -> OfficialCardTranslator:  # noqa: ANN001
    """Assemble the translator from an already-built container.

    Deliberately not a container field, for the same reason the showcase
    service is not: this surface exists only for the Cloud control plane's
    ops calls and borrows the model plumbing the deployment already has.
    """
    provider = getattr(container, "active_llm_provider", None)
    if provider is None:
        raise OfficialCardTranslationUnavailable(
            "no LLM provider is wired on this deployment",
        )
    return OfficialCardTranslator(
        ModelResolver(
            provider=provider,
            feature_key=FEATURE_OFFICIAL_CARD_TRANSLATE,
        ),
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _Absent:
    """Distinguishes "the model omitted this key" from "the model answered
    null" — the first is a dropped field, the second a refusal, and the
    console's wording for the owner differs."""


_ABSENT = _Absent()


def _has_content(value: object) -> bool:
    """Whether a field is worth spending a model call on.

    An empty field is not a failure — it comes back empty with no note,
    because "" translated is still "" and the console must not show the
    owner a problem to retry."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(item, str) and item.strip() for item in value)
    return False


def _build_prompt(
    translatable: Mapping[str, object],
    *,
    target_language: str,
) -> str:
    payload = json.dumps(translatable, ensure_ascii=False, indent=2)
    return (
        f"{TRANSLATE_SYSTEM_PROMPT.rstrip()}\n\n"
        f"Target language: {target_language}\n\n"
        "Input JSON:\n"
        f"{payload}\n\n"
        "Output JSON:"
    )


def _all_untranslated(
    source: dict[str, object],
    translatable: Mapping[str, object],
    reason: str,
) -> OfficialCardTranslation:
    """Every field that had content keeps its source text, each noted.

    Per field rather than one payload-level note so the console renders
    "not configured" and "the model dropped this one field" through the
    same list — the owner reads a per-field state either way.
    """
    return OfficialCardTranslation(
        payload=source,
        notes=tuple(
            TranslationNote(field=name, reason=reason) for name in translatable
        ),
    )


def _merge(
    source: dict[str, object],
    translatable: Mapping[str, object],
    parsed: Mapping[str, object],
) -> OfficialCardTranslation:
    """Accept the model's answer field by field, or keep the source text.

    Which validator applies is decided by the **source value's** type, not
    by which policy tuple the name is in: the caller's payload is what it
    is, and a card whose ``tags`` arrived as one string should not fail
    against a list validator it was never shaped for.
    """
    merged = dict(source)
    notes: list[TranslationNote] = []
    for name, original in translatable.items():
        answer = parsed.get(name, _ABSENT)
        if isinstance(original, list):
            value = (
                None
                if answer is _ABSENT
                else valid_translated_text_list(
                    answer, expected_length=len(original),
                )
            )
            reason = _list_failure_reason(answer, len(original))
        else:
            value = None if answer is _ABSENT else valid_translated_text(answer)
            reason = REASON_MISSING if answer is _ABSENT else REASON_INVALID
        if value is None:
            notes.append(TranslationNote(field=name, reason=reason))
            continue
        merged[name] = value
    return OfficialCardTranslation(payload=merged, notes=tuple(notes))


def _list_failure_reason(answer: object, expected: int) -> str:
    if answer is _ABSENT:
        return REASON_MISSING
    if isinstance(answer, list) and len(answer) != expected:
        return REASON_LENGTH_MISMATCH
    return REASON_INVALID


__all__ = [
    "CARD_META_LIST_FIELDS",
    "CARD_META_SCALAR_FIELDS",
    "REASON_INVALID",
    "REASON_LENGTH_MISMATCH",
    "REASON_LLM_FAILED",
    "REASON_LLM_UNAVAILABLE",
    "REASON_MISSING",
    "TRANSLATABLE_FIELDS",
    "TRANSLATABLE_LIST_FIELDS",
    "TRANSLATABLE_SCALAR_FIELDS",
    "TRANSLATE_SYSTEM_PROMPT",
    "OfficialCardTranslation",
    "OfficialCardTranslationUnavailable",
    "OfficialCardTranslator",
    "TranslationNote",
    "build_official_card_translator",
]
