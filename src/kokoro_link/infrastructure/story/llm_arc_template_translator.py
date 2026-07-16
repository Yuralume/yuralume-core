"""LLM-backed translator for shipped / community arc-template prose.

Mirrors ``infrastructure/character_card/llm_translator.py``: resolve a
model via ``ModelResolver(provider, feature_key)``, short-circuit on a
fake provider, build the prompt from a PromptLoader template plus the
JSON payload, parse fence-tolerantly, and merge field-by-field with
strict same-length validation. Any provider / parse / validation issue
returns the original template (fail-soft) so a translation problem
never blocks binding a valid arc.

Only prose fields are ever sent to the model:

- top-level ``title`` / ``premise``
- each beat's ``title`` / ``summary`` / ``location`` /
  ``scene_characters`` / ``dramatic_question``

Structural fields (``theme`` / ``tone`` / ``tension`` / ``scene_type`` /
``day_offset`` / ``sequence`` / ``required`` / ``duration_days`` /
``world_frames`` / applicability / target ids) are never placed in the
payload — the model cannot see them and therefore cannot reshape the
arc or reinterpret an enum.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.arc_template_translator import (
    ArcTemplateTranslatorPort,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

# Prose fields only — structural fields are deliberately excluded.
_TEMPLATE_SCALAR_FIELDS = ("title", "premise")
_BEAT_SCALAR_FIELDS = (
    "title",
    "summary",
    "location",
    "dramatic_question",
)
_BEAT_LIST_FIELDS = ("scene_characters",)


class LLMArcTemplateTranslator(ArcTemplateTranslatorPort):
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

    async def translate_template(
        self,
        template: ArcTemplate,
        *,
        target_language: str,
    ) -> ArcTemplate:
        target = (target_language or "").strip()
        if not target:
            return template
        if await self._resolver.is_fake():
            return template
        prompt = _build_prompt(template, target_language=target)
        try:
            raw = await self._resolver.generate(prompt)
            parsed = _parse_json_object(raw)
        except Exception:
            _LOGGER.exception(
                "arc template translator: LLM translation failed",
            )
            return template
        merged = _merge_template(template, parsed)
        if merged is template:
            return template
        # Stamp the target language so the picker badge is honest about
        # what the operator is actually reading.
        return merged.with_language(target)


class NullArcTemplateTranslator(ArcTemplateTranslatorPort):
    async def translate_template(
        self,
        template: ArcTemplate,
        *,
        target_language: str,
    ) -> ArcTemplate:
        return template


def _build_prompt(
    template: ArcTemplate,
    *,
    target_language: str,
) -> str:
    tpl = get_default_loader().raw("arc_template/translator").rstrip()
    payload = json.dumps(
        _template_payload(template), ensure_ascii=False, indent=2,
    )
    return (
        f"{tpl}\n\n"
        f"Target language: {target_language}\n\n"
        "Input JSON:\n"
        f"{payload}\n\n"
        "Output JSON:"
    )


def _template_payload(template: ArcTemplate) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in _TEMPLATE_SCALAR_FIELDS:
        payload[field] = getattr(template, field)
    payload["beats"] = [
        {
            "title": beat.title,
            "summary": beat.summary,
            "location": beat.location,
            "scene_characters": list(beat.scene_characters),
            "dramatic_question": beat.dramatic_question,
        }
        for beat in template.beats
    ]
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


def _merge_template(
    template: ArcTemplate,
    parsed: Mapping[str, Any],
) -> ArcTemplate:
    if not parsed:
        return template
    updates: dict[str, Any] = {}
    for field in _TEMPLATE_SCALAR_FIELDS:
        value = _valid_text(parsed.get(field))
        if value is not None:
            updates[field] = value
    beats = _merge_beats(template.beats, parsed.get("beats"))
    if beats is not None:
        updates["beats"] = beats
    if not updates:
        return template
    return replace(template, **updates)


def _merge_beats(
    beats: tuple[ArcTemplateBeat, ...],
    parsed: object,
) -> tuple[ArcTemplateBeat, ...] | None:
    # Strict same-length: the model must return exactly the beats it was
    # given, in order. A count mismatch means the model reshaped the arc
    # — reject the whole beat list and keep the originals (fail-soft).
    if not isinstance(parsed, list) or len(parsed) != len(beats):
        return None
    changed = False
    merged = list(beats)
    for index, raw_item in enumerate(parsed):
        if not isinstance(raw_item, Mapping):
            return None
        beat = beats[index]
        updates: dict[str, Any] = {}
        for field in _BEAT_SCALAR_FIELDS:
            original = getattr(beat, field)
            # Optional prose (location / dramatic_question) that was
            # empty upstream stays empty — never fabricate one.
            if original is None:
                continue
            value = _valid_text(raw_item.get(field))
            if value is not None:
                updates[field] = value
        for field in _BEAT_LIST_FIELDS:
            value = _valid_text_list(
                raw_item.get(field),
                expected_length=len(getattr(beat, field)),
            )
            if value is not None:
                updates[field] = tuple(value)
        if updates:
            merged[index] = replace(beat, **updates)
            changed = True
    return tuple(merged) if changed else None


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
        text = item.strip()
        if not text:
            return None
        cleaned.append(text)
    return cleaned


__all__ = [
    "LLMArcTemplateTranslator",
    "NullArcTemplateTranslator",
]
