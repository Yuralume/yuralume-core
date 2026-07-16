"""LLM-backed translator for ``.lumecard`` A-layer profile text."""

from __future__ import annotations

import json
import logging
import re
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

_LOGGER = logging.getLogger(__name__)

_PROFILE_SCALAR_FIELDS = (
    "name",
    "summary",
    "speaking_style",
    "appearance",
    "gender_identity",
    "third_person_pronoun",
    "visual_gender_presentation",
)
_PROFILE_LIST_FIELDS = (
    "personality",
    "interests",
    "boundaries",
    "aspirations",
    "world_topics",
    "excluded_topics",
)
_COMPANION_SCALAR_FIELDS = (
    "name",
    "role",
    "brief_profile",
    "relationship_snippet",
)
_COMPANION_LIST_FIELDS = ("personality_sketch",)


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
        return _merge_profile(profile, parsed)


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
    for field in _PROFILE_SCALAR_FIELDS + _PROFILE_LIST_FIELDS:
        payload[field] = getattr(profile, field)
    payload["companions"] = [
        {
            field: getattr(companion, field)
            for field in _COMPANION_SCALAR_FIELDS + _COMPANION_LIST_FIELDS
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


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_json_object(raw: str) -> Mapping[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    text = _FENCE_RE.sub("", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, Mapping) else {}


def _merge_profile(
    profile: CharacterCardProfile,
    parsed: Mapping[str, Any],
) -> CharacterCardProfile:
    if not parsed:
        return profile
    updates: dict[str, Any] = {}
    for field in _PROFILE_SCALAR_FIELDS:
        value = _valid_text(parsed.get(field))
        if value is not None:
            updates[field] = value
    for field in _PROFILE_LIST_FIELDS:
        value = _valid_text_list(
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
    rationale = _valid_text(parsed.get("rationale"))
    if rationale is not None:
        updates["rationale"] = rationale
    notes = _valid_text_list(
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
        for field in _COMPANION_SCALAR_FIELDS:
            value = _valid_text(raw_item.get(field))
            if value is not None:
                updates[field] = value
        for field in _COMPANION_LIST_FIELDS:
            value = _valid_text_list(
                raw_item.get(field),
                expected_length=len(getattr(companion, field)),
            )
            if value is not None:
                updates[field] = value
        if updates:
            merged[index] = companion.model_copy(update=updates)
            changed = True
    return merged if changed else None


def _valid_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _valid_text_list(value: object, *, expected_length: int) -> list[str] | None:
    if not isinstance(value, list) or len(value) != expected_length:
        return None
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        cleaned.append(item.strip())
    return cleaned


__all__ = [
    "LLMCharacterCardTranslator",
    "NullCharacterCardTranslator",
]
