"""LLM-backed localizer for player-visible memoir text."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.memoir_localizer import MemoirLocalizerPort
from kokoro_link.domain.entities.memoir import (
    MemoirChapter,
    MemoirEntry,
    MemoirView,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_VISIBLE_EXTRA_KEYS = frozenset({"tags", "emotion_label"})


class LLMMemoirLocalizer(MemoirLocalizerPort):
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

    async def localize_view(
        self,
        view: MemoirView,
        *,
        target_language: str,
    ) -> MemoirView:
        target = (target_language or "").strip()
        if not target:
            return view
        if not view.chapters and not view.timeline:
            return view
        if await self._resolver.is_fake():
            return view
        prompt = _build_prompt(view, target_language=target)
        try:
            raw = await self._resolver.generate(prompt)
            parsed = _parse_json_object(raw)
        except Exception:
            _LOGGER.exception("memoir localizer: LLM localisation failed")
            return view
        return _merge_view(view, parsed)


class NullMemoirLocalizer(MemoirLocalizerPort):
    async def localize_view(
        self,
        view: MemoirView,
        *,
        target_language: str,
    ) -> MemoirView:
        return view


def _build_prompt(view: MemoirView, *, target_language: str) -> str:
    template = get_default_loader().raw("memoir/localizer").rstrip()
    payload = json.dumps(_view_payload(view), ensure_ascii=False, indent=2)
    return (
        f"{template}\n\n"
        f"Target language: {target_language}\n\n"
        "Input JSON:\n"
        f"{payload}\n\n"
        "Output JSON:"
    )


def _view_payload(view: MemoirView) -> dict[str, Any]:
    return {
        "chapters": [
            {
                "index": index,
                "period": chapter.period,
                "narrative": chapter.narrative,
                "dominant_themes": list(chapter.dominant_themes),
                "evidence_quotes": list(chapter.evidence_quotes),
            }
            for index, chapter in enumerate(view.chapters)
        ],
        "timeline": [
            {
                "index": index,
                "kind": entry.kind,
                "summary": entry.summary,
                "extras": {
                    key: value
                    for key, value in entry.extras.items()
                    if key in _VISIBLE_EXTRA_KEYS
                },
            }
            for index, entry in enumerate(view.timeline)
        ],
    }


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


def _merge_view(view: MemoirView, parsed: Mapping[str, Any]) -> MemoirView:
    if not parsed:
        return view
    chapters = _merge_chapters(view.chapters, parsed.get("chapters"))
    timeline = _merge_timeline(view.timeline, parsed.get("timeline"))
    if chapters == view.chapters and timeline == view.timeline:
        return view
    return MemoirView(
        chapters=chapters,
        timeline=timeline,
        pin_count=view.pin_count,
        pin_limit=view.pin_limit,
    )


def _merge_chapters(
    chapters: tuple[MemoirChapter, ...],
    parsed: object,
) -> tuple[MemoirChapter, ...]:
    if not isinstance(parsed, list):
        return chapters
    merged = list(chapters)
    for raw_item in parsed:
        if not isinstance(raw_item, Mapping):
            continue
        index = _valid_index(raw_item.get("index"), len(chapters))
        if index is None:
            continue
        chapter = chapters[index]
        narrative = _valid_text(raw_item.get("narrative"))
        themes = _valid_text_list(raw_item.get("dominant_themes"))
        quotes = _valid_text_list(raw_item.get("evidence_quotes"))
        updates: dict[str, Any] = {}
        if narrative is not None:
            updates["narrative"] = narrative
        if themes is not None:
            updates["dominant_themes"] = tuple(themes)
        if quotes is not None:
            updates["evidence_quotes"] = tuple(quotes)
        if updates:
            merged[index] = MemoirChapter(
                period=chapter.period,
                period_start=chapter.period_start,
                period_end=chapter.period_end,
                narrative=updates.get("narrative", chapter.narrative),
                dominant_themes=updates.get(
                    "dominant_themes", chapter.dominant_themes,
                ),
                evidence_quotes=updates.get(
                    "evidence_quotes", chapter.evidence_quotes,
                ),
            )
    return tuple(merged)


def _merge_timeline(
    entries: tuple[MemoirEntry, ...],
    parsed: object,
) -> tuple[MemoirEntry, ...]:
    if not isinstance(parsed, list):
        return entries
    merged = list(entries)
    for raw_item in parsed:
        if not isinstance(raw_item, Mapping):
            continue
        index = _valid_index(raw_item.get("index"), len(entries))
        if index is None:
            continue
        entry = entries[index]
        summary = _valid_text(raw_item.get("summary"))
        extras = _merge_visible_extras(entry.extras, raw_item.get("extras"))
        if summary is None and extras is None:
            continue
        merged[index] = MemoirEntry(
            kind=entry.kind,
            entry_id=entry.entry_id,
            occurred_at=entry.occurred_at,
            summary=summary or entry.summary,
            score=entry.score,
            pinned=entry.pinned,
            extras=extras or entry.extras,
        )
    return tuple(merged)


def _merge_visible_extras(
    original: Mapping[str, str],
    parsed: object,
) -> dict[str, str] | None:
    if not isinstance(parsed, Mapping):
        return None
    merged = dict(original)
    changed = False
    for key in _VISIBLE_EXTRA_KEYS:
        if key not in parsed:
            continue
        value = _valid_text(parsed.get(key))
        if value is None:
            continue
        merged[key] = value
        changed = True
    return merged if changed else None


def _valid_index(value: object, length: int) -> int | None:
    if not isinstance(value, int):
        return None
    if value < 0 or value >= length:
        return None
    return value


def _valid_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _valid_text_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    cleaned: list[str] = []
    for item in value:
        text = _valid_text(item)
        if text is None:
            return None
        cleaned.append(text)
    return cleaned


__all__ = [
    "LLMMemoirLocalizer",
    "NullMemoirLocalizer",
]
