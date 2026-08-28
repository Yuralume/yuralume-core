"""LLM-backed translator for story-seed one-liner prompts.

One-shot CLI batch: send all seed texts in a single JSON payload, parse
fence-tolerantly, and require the same count back (a mismatch means the
model dropped / added a line, so we reject the whole batch and keep the
originals). Fail-soft throughout — a fake provider or any error returns
the inputs unchanged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story_seed_translator import StorySeedTranslatorPort
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.llm_output import extract_object_outcome, log_parse_outcome

_LOGGER = logging.getLogger(__name__)


class LLMStorySeedTranslator(StorySeedTranslatorPort):
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

    async def translate_seed_texts(
        self,
        seed_texts: Sequence[str],
        *,
        target_language: str,
    ) -> list[str]:
        originals = list(seed_texts)
        target = (target_language or "").strip()
        if not target or not originals:
            return originals
        if await self._resolver.is_fake():
            return originals
        prompt = _build_prompt(originals, target_language=target)
        try:
            raw = await self._resolver.generate(prompt)
            parsed = _parse_json_object(raw)
        except Exception:
            _LOGGER.exception("story seed translator: LLM translation failed")
            return originals
        translated = _extract_seeds(parsed, expected_length=len(originals))
        if translated is None:
            return originals
        # Merge element-wise: keep the original when a slot came back
        # blank so a single miss doesn't blank a seed.
        return [
            new or old for old, new in zip(originals, translated)
        ]


class NullStorySeedTranslator(StorySeedTranslatorPort):
    async def translate_seed_texts(
        self,
        seed_texts: Sequence[str],
        *,
        target_language: str,
    ) -> list[str]:
        return list(seed_texts)


def _build_prompt(
    seed_texts: Sequence[str],
    *,
    target_language: str,
) -> str:
    tpl = get_default_loader().raw("story_seed/translator").rstrip()
    payload = json.dumps(
        {"seeds": list(seed_texts)}, ensure_ascii=False, indent=2,
    )
    return (
        f"{tpl}\n\n"
        f"Target language: {target_language}\n\n"
        "Input JSON:\n"
        f"{payload}\n\n"
        "Output JSON:"
    )


def _parse_json_object(raw: str) -> Mapping[str, Any]:
    """Best-effort object extraction; ``{}`` (not ``None``) on any failure —
    the caller treats a falsy ``parsed`` as "keep the original seed texts".

    Truncation repair off (FX1/DH-3): ``_extract_seeds`` only checks the
    list's *length* and each item's type, so a final seed cut
    mid-sentence is accepted and stored. See
    ``character_card/llm_translator._parse_json_object`` for the
    reasoning the five translator sites share.
    """
    outcome = extract_object_outcome(raw, repair_truncated=False)
    log_parse_outcome(_LOGGER, outcome, site="story.llm_story_seed_translator")
    return outcome.value if outcome.value is not None else {}


def _extract_seeds(
    parsed: Mapping[str, Any], *, expected_length: int,
) -> list[str] | None:
    seeds = parsed.get("seeds")
    if not isinstance(seeds, list) or len(seeds) != expected_length:
        return None
    out: list[str] = []
    for item in seeds:
        if not isinstance(item, str):
            return None
        out.append(item.strip())
    return out


__all__ = [
    "LLMStorySeedTranslator",
    "NullStorySeedTranslator",
]
