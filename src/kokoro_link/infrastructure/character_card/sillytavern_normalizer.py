"""LLM-backed + null adapters for the SillyTavern normalizer port.

Co-located with ``llm_translator.py`` (the other character-card LLM
adapter) rather than under ``infrastructure/llm/`` so the whole card
front-layer lives in one package.

The LLM adapter follows the ``character_draft`` / ``llm_translator``
pattern: a :class:`ModelResolver` wraps the active provider under a
feature key, the prompt comes from the external prompt pack, and every
failure path degrades to a best-effort profile (D4 fail-open: raw
description becomes ``summary``, structured fields stay empty) instead of
raising, so a normalization hiccup never sinks a valid import.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.sillytavern_normalizer import (
    SillyTavernNormalizedProfile,
    SillyTavernNormalizerInput,
    SillyTavernNormalizerPort,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_SUMMARY_CHARS = 600
_MAX_APPEARANCE_CHARS = 600
_MAX_STYLE_CHARS = 200
_MAX_CONTEXT_CHARS = 600
_MAX_LIST_ITEMS = 6
_MAX_LIST_ITEM_CHARS = 60
# The card prose can be long; cap what we feed the model so one giant
# card can't blow the context budget. The full card is still available
# downstream — this only bounds the normalization input.
_MAX_FIELD_INPUT_CHARS = 6000

_SCALAR_FIELDS = ("summary", "appearance", "speaking_style", "suggested_known_context")
_LIST_FIELDS = ("personality", "interests", "boundaries", "aspirations")


class LLMSillyTavernNormalizer(SillyTavernNormalizerPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def normalize(
        self,
        payload: SillyTavernNormalizerInput,
        *,
        operator_id: str | None = None,
    ) -> SillyTavernNormalizedProfile:
        fallback = _fallback_profile(payload)
        try:
            if await self._resolver.is_fake(operator_id=operator_id):
                return fallback
        except Exception:  # pragma: no cover — defensive
            _LOGGER.exception("sillytavern normalizer: is_fake probe failed")
            return fallback

        prompt = _build_prompt(payload)
        try:
            raw = await self._resolver.generate(prompt, operator_id=operator_id)
        except Exception:
            _LOGGER.exception("sillytavern normalizer: LLM generation failed")
            return fallback
        parsed = _parse_json_object(raw)
        if not parsed:
            return fallback
        return _coerce_profile(parsed, fallback=fallback)


class NullSillyTavernNormalizer(SillyTavernNormalizerPort):
    """Deterministic fallback used when no capable LLM is wired.

    Mirrors the LLM adapter's fail-open contract: the raw description
    becomes the summary and the scenario becomes the suggested context,
    so a self-host deployment without an LLM still imports the card with
    usable (if unstructured) text the operator can refine."""

    async def normalize(
        self,
        payload: SillyTavernNormalizerInput,
        *,
        operator_id: str | None = None,
    ) -> SillyTavernNormalizedProfile:
        return _fallback_profile(payload)


def _fallback_profile(
    payload: SillyTavernNormalizerInput,
) -> SillyTavernNormalizedProfile:
    return SillyTavernNormalizedProfile(
        summary=payload.description.strip()[:_MAX_SUMMARY_CHARS],
        suggested_known_context=payload.scenario.strip()[:_MAX_CONTEXT_CHARS],
    )


def _build_prompt(payload: SillyTavernNormalizerInput) -> str:
    template = get_default_loader().raw(
        "character_card/sillytavern_normalizer",
    ).rstrip()
    language_hint = render_operator_language_hint(payload.operator_primary_language)
    card_payload = {
        "name": payload.name[:_MAX_FIELD_INPUT_CHARS],
        "description": payload.description[:_MAX_FIELD_INPUT_CHARS],
        "personality": payload.personality[:_MAX_FIELD_INPUT_CHARS],
        "scenario": payload.scenario[:_MAX_FIELD_INPUT_CHARS],
        "mes_example": payload.mes_example[:_MAX_FIELD_INPUT_CHARS],
        "first_mes": payload.first_mes[:_MAX_FIELD_INPUT_CHARS],
    }
    body = json.dumps(card_payload, ensure_ascii=False, indent=2)
    header = f"{language_hint}\n\n" if language_hint else ""
    return (
        f"{header}{template}\n\n"
        "SillyTavern card fields:\n"
        f"{body}\n\n"
        "Output JSON:"
    )


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


def _coerce_profile(
    parsed: Mapping[str, Any],
    *,
    fallback: SillyTavernNormalizedProfile,
) -> SillyTavernNormalizedProfile:
    summary = _text(parsed.get("summary"), _MAX_SUMMARY_CHARS) or fallback.summary
    context = (
        _text(parsed.get("suggested_known_context"), _MAX_CONTEXT_CHARS)
        or fallback.suggested_known_context
    )
    return SillyTavernNormalizedProfile(
        summary=summary,
        personality=_text_list(parsed.get("personality")),
        interests=_text_list(parsed.get("interests")),
        boundaries=_text_list(parsed.get("boundaries")),
        aspirations=_text_list(parsed.get("aspirations")),
        appearance=_text(parsed.get("appearance"), _MAX_APPEARANCE_CHARS),
        speaking_style=_text(parsed.get("speaking_style"), _MAX_STYLE_CHARS),
        suggested_known_context=context,
    )


def _text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_chars]


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, (str, int, float)):
            continue
        text = str(item).strip()[:_MAX_LIST_ITEM_CHARS]
        if text:
            out.append(text)
        if len(out) >= _MAX_LIST_ITEMS:
            break
    return out


__all__ = [
    "LLMSillyTavernNormalizer",
    "NullSillyTavernNormalizer",
]
