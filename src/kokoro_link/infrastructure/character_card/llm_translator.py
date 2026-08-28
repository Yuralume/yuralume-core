"""LLM-backed translator for ``.lumecard`` A-layer profile text."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from kokoro_link.application.dto.character import CharacterCompanionPayload
from kokoro_link.application.dto.character_card import CharacterCardProfile
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.character_card_translator import (
    CharacterCardTranslatorPort,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.llm_output import extract_object_outcome, log_parse_outcome

_LOGGER = logging.getLogger(__name__)

PROFILE_SCALAR_FIELDS = (
    "name",
    "summary",
    "speaking_style",
    "appearance",
    "gender_identity",
    "third_person_pronoun",
    "visual_gender_presentation",
)
"""The A-layer profile prose a translator may rewrite. Public because it is
the *field policy* for card prose, not an implementation detail of this
adapter — the Cloud official-card catalog translates the same set through
its own ops endpoint, and two copies of this tuple would drift the moment
someone adds a field to :class:`CharacterCardProfile`."""

PROFILE_LIST_FIELDS = (
    "personality",
    "interests",
    "boundaries",
    "aspirations",
    "world_topics",
    "excluded_topics",
)
"""Prose list fields. Same-length replacement only — see
:func:`valid_translated_text_list`."""

COMPANION_SCALAR_FIELDS = (
    "name",
    "role",
    "brief_profile",
    "relationship_snippet",
)
COMPANION_LIST_FIELDS = ("personality_sketch",)
PERSONALITY_TYPE_SCALAR_FIELDS = ("rationale",)
PERSONALITY_TYPE_LIST_FIELDS = ("consistency_notes",)


class LLMCharacterCardTranslator(CharacterCardTranslatorPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def translate_profile(
        self,
        profile: CharacterCardProfile,
        *,
        target_language: str,
    ) -> CharacterCardProfile:
        target = (target_language or "").strip()
        if not target:
            return profile
        if await self._resolver.is_fake():
            return profile
        prompt = _build_prompt(profile, target_language=target)
        try:
            raw = await self._resolver.generate(prompt)
            parsed = _parse_json_object(raw)
        except Exception:
            _LOGGER.exception(
                "character card translator: LLM translation failed",
            )
            return profile
        return merge_translated_profile(profile, parsed)


class NullCharacterCardTranslator(CharacterCardTranslatorPort):
    async def translate_profile(
        self,
        profile: CharacterCardProfile,
        *,
        target_language: str,
    ) -> CharacterCardProfile:
        return profile


def _build_prompt(
    profile: CharacterCardProfile,
    *,
    target_language: str,
) -> str:
    template = get_default_loader().raw("character_card/translator").rstrip()
    payload = json.dumps(_profile_payload(profile), ensure_ascii=False, indent=2)
    return (
        f"{template}\n\n"
        f"Target language: {target_language}\n\n"
        "Input JSON:\n"
        f"{payload}\n\n"
        "Output JSON:"
    )


def _profile_payload(profile: CharacterCardProfile) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in PROFILE_SCALAR_FIELDS + PROFILE_LIST_FIELDS:
        payload[field] = getattr(profile, field)
    payload["companions"] = [
        {
            field: getattr(companion, field)
            for field in COMPANION_SCALAR_FIELDS + COMPANION_LIST_FIELDS
        }
        for companion in profile.companions
    ]
    if profile.personality_type.code:
        payload["personality_type"] = {
            "code": profile.personality_type.code,
            "rationale": profile.personality_type.rationale,
            "consistency_notes": list(profile.personality_type.consistency_notes),
        }
    return payload


def _parse_json_object(raw: str) -> Mapping[str, Any]:
    """Best-effort object extraction; ``{}`` (not ``None``) on any failure.

    The empty-dict sentinel is load-bearing for callers downstream
    (``merge_translated_profile`` treats a falsy ``parsed`` as "nothing
    to merge") — it predates the shared layer and stays unchanged here.

    **Truncation repair is off, and this is the canonical reason for the
    five translator sites that share this shape** (this module,
    ``character_card/sillytavern_normalizer``, ``memoir/llm_localizer``,
    ``story/llm_arc_template_translator``,
    ``story/llm_story_seed_translator``). What repair recovers is *text
    that stopped mid-sentence*: it closes the dangling string and hands
    back a value that is a perfectly good ``str``, so every type check
    downstream passes and the half-summary is merged over the source
    prose and **persisted** — into a character card, a template, a seed.
    There is no later step that notices. The pre-migration code returned
    ``{}`` here and the original text survived; a translation that
    silently loses its second half is strictly worse than an untranslated
    one. Same fail-closed call the feed composer, the video storyboard
    and the proactive decider each made, for the same reason: repair
    belongs where a partial answer is still an answer, not where it
    overwrites something correct.
    """
    outcome = extract_object_outcome(raw, repair_truncated=False)
    log_parse_outcome(_LOGGER, outcome, site="character_card.llm_translator")
    return outcome.value if outcome.value is not None else {}


def merge_translated_profile(
    profile: CharacterCardProfile,
    parsed: Mapping[str, Any],
) -> CharacterCardProfile:
    """Lay a translated payload over a profile, field by field.

    Public for the same reason the field tuples above are: this *is* the
    merge policy for card prose — same-length lists or nothing, never blank
    the source, never touch structure — and the official-card path applies
    the very same policy to a payload Cloud translated ahead of time. A
    second implementation there would drift on the first new profile field.
    """
    if not parsed:
        return profile
    updates: dict[str, Any] = {}
    for field in PROFILE_SCALAR_FIELDS:
        value = valid_translated_text(parsed.get(field))
        if value is not None:
            updates[field] = value
    for field in PROFILE_LIST_FIELDS:
        value = valid_translated_text_list(
            parsed.get(field),
            expected_length=len(getattr(profile, field)),
        )
        if value is not None:
            updates[field] = value
    companions = _merge_companions(profile.companions, parsed.get("companions"))
    if companions is not None:
        updates["companions"] = companions
    personality_type = _merge_personality_type(
        profile.personality_type,
        parsed.get("personality_type"),
    )
    if personality_type is not None:
        updates["personality_type"] = personality_type
    if not updates:
        return profile
    return profile.model_copy(update=updates)


def _merge_personality_type(
    personality_type,
    parsed: object,
):
    if not isinstance(parsed, Mapping) or not personality_type.code:
        return None
    updates: dict[str, Any] = {}
    rationale = valid_translated_text(parsed.get("rationale"))
    if rationale is not None:
        updates["rationale"] = rationale
    notes = valid_translated_text_list(
        parsed.get("consistency_notes"),
        expected_length=len(personality_type.consistency_notes),
    )
    if notes is not None:
        updates["consistency_notes"] = notes
    if not updates:
        return None
    # Deliberately preserve code/source/confidence. The translator may
    # localize explanatory prose only; it must not reinterpret the type.
    return personality_type.model_copy(update=updates)


def _merge_companions(
    companions: list[CharacterCompanionPayload],
    parsed: object,
) -> list[CharacterCompanionPayload] | None:
    if not isinstance(parsed, list):
        return None
    changed = False
    merged = list(companions)
    for index, raw_item in enumerate(parsed[: len(companions)]):
        if not isinstance(raw_item, Mapping):
            continue
        companion = companions[index]
        updates: dict[str, Any] = {}
        for field in COMPANION_SCALAR_FIELDS:
            value = valid_translated_text(raw_item.get(field))
            if value is not None:
                updates[field] = value
        for field in COMPANION_LIST_FIELDS:
            value = valid_translated_text_list(
                raw_item.get(field),
                expected_length=len(getattr(companion, field)),
            )
            if value is not None:
                updates[field] = value
        if updates:
            merged[index] = companion.model_copy(update=updates)
            changed = True
    return merged if changed else None


def valid_translated_text(value: object) -> str | None:
    """The model's replacement for one scalar field, or ``None``.

    ``None`` means "keep the source text": a non-string or an empty answer
    is a failed field, never an instruction to blank the original."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def valid_translated_text_list(
    value: object,
    *,
    expected_length: int,
) -> list[str] | None:
    """The model's replacement for one list field, or ``None``.

    A list that came back the wrong length is rejected **whole**: pairing
    item 2 with item 3's translation is worse than staying in the source
    language, and there is no way to tell which item was dropped."""
    if not isinstance(value, list) or len(value) != expected_length:
        return None
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        cleaned.append(item.strip())
    return cleaned


__all__ = [
    "COMPANION_LIST_FIELDS",
    "COMPANION_SCALAR_FIELDS",
    "PERSONALITY_TYPE_LIST_FIELDS",
    "PERSONALITY_TYPE_SCALAR_FIELDS",
    "PROFILE_LIST_FIELDS",
    "PROFILE_SCALAR_FIELDS",
    "LLMCharacterCardTranslator",
    "NullCharacterCardTranslator",
    "merge_translated_profile",
    "valid_translated_text",
    "valid_translated_text_list",
]
