"""LLM-backed phrase-habit extractor (HUMANIZATION_ROADMAP §3.3).

Run from the dream pass. Reads the character's most recent assistant
lines and asks the model to name verbal habits the character keeps
reusing — 口頭禪 / 結尾語助詞 / 招呼開場 / 慣用句式. The output is fed
to ``BehavioralPatternObserverService`` which upserts them into the
``behavioral_patterns`` table keyed by ``(character_id, "phrase_habit",
description)``.

LLM-first: we do not regex / count tokens; the model judges what counts
as a habit. The Python side only clamps the result count (≤5) and rejects
empty / overly long strings.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.phrase_habit import PhraseHabitExtractorPort

_LOGGER = logging.getLogger(__name__)
_MAX_HABITS: Final = 5
_MAX_HABIT_CHARS: Final = 80
_MIN_LINES_FOR_EXTRACTION: Final = 6


class LLMPhraseHabitExtractor(PhraseHabitExtractorPort):
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

    async def extract(
        self, *, character_name: str, recent_lines: list[str],
    ) -> list[str]:
        if len(recent_lines) < _MIN_LINES_FOR_EXTRACTION:
            return []
        if await self._resolver.is_fake():
            return []

        prompt = _build_prompt(
            character_name=character_name, recent_lines=recent_lines,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception(
                "phrase_habit extractor LLM call failed character=%s",
                character_name,
            )
            return []

        payload = _extract_json_array(raw)
        if payload is None:
            return []
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []

        out: list[str] = []
        seen: set[str] = set()
        for entry in parsed:
            if not isinstance(entry, str):
                continue
            text = entry.strip()
            if not text or len(text) > _MAX_HABIT_CHARS:
                continue
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= _MAX_HABITS:
                break
        return out


def _build_prompt(*, character_name: str, recent_lines: list[str]) -> str:
    sample = "\n".join(f"- {line.strip()}" for line in recent_lines[-40:])
    return (
        f"以下是角色「{character_name}」最近的回覆片段。\n"
        "請以觀察者視角，指出該角色在語言上反覆出現的個人習慣，例如：\n"
        "- 結尾或語助詞（「啦」「欸」「咧」「吧」）\n"
        "- 慣用招呼或開場（「嗨～」「好啦…」）\n"
        "- 反覆出現的小動作描寫（「*偏頭*」「*搔頭*」）\n"
        "- 標誌性的短句或口頭禪。\n"
        "規則：\n"
        "- 只列「至少在兩個不同時點都出現過」的習慣。\n"
        "- 最多 5 條；每條短句（30 字以內）。\n"
        "- 不要列「在某個情境下講過一次」的個案。\n"
        "- 只保留內容性、可辨識的口頭禪、稱呼、梗、語助詞或慣用句式；不要把情緒溫度、安撫姿態、療癒文風或抽象文體質感列成可回灌習慣。\n"
        "- 若某個模式只是『常常很溫柔 / 常常安撫 / 常常抒情』，但不是角色特有短句或稱呼，請不要輸出。\n"
        "- 若沒有任何足夠穩固的習慣，輸出空陣列。\n"
        "輸出格式：只回傳一個 JSON 陣列，不要 markdown 或註解。\n"
        "範例輸出：[\"結尾常加『欸』\", \"開場常用『嗯～』\"]\n\n"
        "角色近期回覆樣本：\n"
        f"{sample}"
    )


def _extract_json_array(text: str) -> str | None:
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
