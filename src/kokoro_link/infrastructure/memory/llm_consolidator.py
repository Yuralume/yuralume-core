"""LLM-backed memory consolidator.

Given a cluster of near-duplicate memories, asks the model to emit a
single JSON object merging them. The prompt is Chinese-first and
instructs the model to:

- preserve the first-person voice of the original memories
- fuse overlapping facts instead of stacking them
- pick the narrowest accurate kind (if the cluster is already all one
  kind, that kind wins by construction — clustering never crosses kinds)
- output clean JSON with no code fences

Malformed output is silently discarded (``merge`` returns ``None``)
so callers leave the cluster intact instead of corrupting it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.memory_consolidator import (
    MemoryConsolidatorPort,
    MergeProposal,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 280
_MAX_TAGS = 8
_MAX_TAG_CHARS = 40


class LLMMemoryConsolidator(MemoryConsolidatorPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def merge(
        self,
        cluster: list[MemoryItem],
        *,
        character: Character | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> MergeProposal | None:
        if len(cluster) < 2:
            return None
        if await self._resolver.is_fake(character=character):
            return None
        prompt = _build_prompt(
            cluster, operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception("Consolidator LLM call failed")
            return None

        parsed = _extract_object(raw)
        if parsed is None:
            return None
        return _coerce_proposal(parsed, fallback_kind=cluster[0].kind)


def _build_prompt(
    cluster: list[MemoryItem],
    *,
    operator_primary_language: str = "zh-TW",
) -> str:
    kind_value = cluster[0].kind.value
    highest_salience = max(item.salience for item in cluster)
    bullet_lines = "\n".join(f"- {item.content}" for item in cluster)
    return get_default_loader().render(
        "memory/consolidator",
        # Merged content shows in MemoryBrowserPanel, so pin it to the
        # operator's content language instead of the old "中文為主" bias
        # that re-Sinicised English / Japanese source memories.
        language_hint=render_operator_language_hint(operator_primary_language),
        kind_value=kind_value,
        bullet_lines=bullet_lines,
        highest_salience=f"{highest_salience:.2f}",
    )


def _extract_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _coerce_proposal(
    payload: dict[str, Any],
    *,
    fallback_kind: MemoryKind,
) -> MergeProposal | None:
    content_raw = payload.get("content")
    if not isinstance(content_raw, str):
        return None
    content = content_raw.strip()[:_MAX_CONTENT_CHARS]
    if not content:
        return None

    salience_raw = payload.get("salience", 0.6)
    try:
        salience = float(salience_raw)
    except (TypeError, ValueError):
        salience = 0.6
    salience = max(0.0, min(1.0, salience))

    tags_raw = payload.get("tags")
    tags: list[str] = []
    if isinstance(tags_raw, list):
        for tag in tags_raw:
            if not isinstance(tag, (str, int, float)):
                continue
            text = str(tag).strip().lower()[:_MAX_TAG_CHARS]
            if text and text not in tags:
                tags.append(text)
            if len(tags) >= _MAX_TAGS:
                break

    return MergeProposal(
        content=content,
        kind=fallback_kind,
        salience=salience,
        tags=tuple(tags),
    )
