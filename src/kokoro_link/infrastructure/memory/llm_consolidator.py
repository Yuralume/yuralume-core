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
so callers leave the cluster intact instead of corrupting it. That
includes output the shared extractor *could* have repaired — see the
comment at the extraction call for why this site declines the repair.
"""

from __future__ import annotations

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
from kokoro_link.llm_output import extract_object_outcome, log_parse_outcome

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

        # Truncation repair stays off here, and the reason is the
        # *consequence*, not the payload shape. A merge proposal is not
        # read and forgotten: ``_consolidate_cluster`` writes it as a
        # new memory and then deletes every original in the cluster. So
        # a reply cut mid-``content`` — ``{"content": "上週跟朋友約好，
        # 下班後要一起去藍調酒吧`` — is not a degraded read, it is a
        # half-sentence that replaces the whole-sentence memories it was
        # built from, permanently and with nothing left to compare it
        # against. Repair closes the dangling string, so the merged text
        # arrives as a perfectly ordinary ``str`` and nothing downstream
        # can tell it was cut.
        #
        # Failing closed costs one cluster one pass: ``merge`` returns
        # ``None``, ``consolidate`` skips it, the originals stay, and the
        # next consolidation run tries the same cluster again. Same trade
        # the translator sites made (DH-3), for the same reason — the
        # write is destructive and the retry is free.
        outcome = extract_object_outcome(raw, repair_truncated=False)
        log_parse_outcome(_LOGGER, outcome, site="memory.llm_consolidator")
        parsed = outcome.value
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
